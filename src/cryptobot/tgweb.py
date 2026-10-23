"""Telegram's public web previews, scraped politely: no account, no login, no API key.

Telegram serves a public preview of every public channel and group (https://t.me/<name>
shows the member count; https://t.me/s/<name> the latest ~20 posts) to anyone, and
t.me has no robots.txt rules against it. This reads those pages as plain HTML, slowly
(TGWEB_GAP_S between requests), with a descriptive User-Agent, and stops on any error
rather than retrying hard. It never logs in, never posts, and does nothing to get past
a block: if Telegram stops serving previews, the source goes quiet.

Two uses in social.py:
- **The coin's own Telegram.** For each watched coin, its metadata's Telegram link (if
  any) gives `tg_members` (members or subscribers) and `tg_posts_1h` (posts in the last
  hour among the latest shown), a free read on how alive its community is.
- **Call channels** the owner lists in TELEGRAM_WEB_CHANNELS (public @names, comma
  separated): new posts are counted for mint addresses and $TICKERs (`tgweb_mint`,
  `tgweb_ticker`), like Bluesky. Unlike telegram_feed.py this needs no account.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.request
from datetime import datetime

from cryptobot.holders import fetch_metadata

USER_AGENT = (
    "Mozilla/5.0 (compatible; crypto-bot-research/0.1; +https://github.com/envyllcompanies-lang)"
)
TGWEB_GAP_S = 5.0
HANDLE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?$")
MEMBERS = re.compile(r'tgme_page_extra">\s*([\d\s,.]+)\s*(subscribers|members)')
POST_ID = re.compile(r'data-post="([^"]+)"')
TEXT = re.compile(r"tgme_widget_message_text[^>]*>(.*?)</div>", re.S)
TIME = re.compile(r'<time datetime="([^"]+)"')
TAG = re.compile(r"<[^>]+>")


def handle_of(link: object) -> str | None:
    """The public @name in a t.me link, or None (invite links and bots don't count)."""
    if not isinstance(link, str):
        return None
    m = HANDLE.match(link.strip())
    if not m or m.group(1).lower().endswith("bot") or m.group(1).startswith("joinchat"):
        return None
    return m.group(1)


def members(page: str) -> int | None:
    m = MEMBERS.search(page)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def posts(page: str) -> list[tuple[str, str, float]]:
    """(post id, text, unix seconds) for each post on a /s/ preview page."""
    out = []
    for block in page.split("tgme_widget_message_wrap")[1:]:  # one block per post
        pid, stamp = POST_ID.search(block), TIME.search(block)
        if not pid or not stamp:
            continue
        try:
            ts = datetime.fromisoformat(stamp.group(1)).timestamp()
        except ValueError:
            continue
        text = TEXT.search(block)
        body = html.unescape(TAG.sub(" ", text.group(1))) if text else ""
        out.append((pid.group(1), body, ts))
    return out


class TgWeb:
    def __init__(self) -> None:
        self.last = 0.0
        self.seen: dict[str, set[str]] = {}  # channel -> post ids already counted

    def ready(self) -> bool:
        return time.time() - self.last >= TGWEB_GAP_S

    def _get(self, url: str) -> str:
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(2_000_000).decode("utf-8", "replace")

    def coin(self, uri: str | None) -> dict | None:
        """Members and recent posts of the Telegram a coin's metadata links to."""
        meta = fetch_metadata(uri or "")
        handle = handle_of((meta or {}).get("telegram"))
        if handle is None:
            return None
        out: dict = {}
        n = members(self._get(f"https://t.me/{handle}"))
        if n is not None:
            out["tg_members"] = float(n)
        time.sleep(TGWEB_GAP_S)
        recent = [
            p for p in posts(self._get(f"https://t.me/s/{handle}")) if p[2] > time.time() - 3600
        ]
        out["tg_posts_1h"] = float(len(recent))
        return out

    def new_posts(self, channel: str) -> list[str]:
        """Texts of posts in a public channel not seen on earlier visits."""
        page = self._get(f"https://t.me/s/{channel}")
        seen = self.seen.setdefault(channel, set())
        first_visit = not seen
        out = []
        for post_id, text, _ in posts(page):
            if post_id not in seen:
                seen.add(post_id)
                if not first_visit:  # the first visit only sets the baseline
                    out.append(text)
        if len(seen) > 500:
            self.seen[channel] = set(sorted(seen)[-200:])
        return out


def channels() -> list[str]:
    raw = os.environ.get("TELEGRAM_WEB_CHANNELS", "")
    return [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]


def dumps(item: dict) -> str:
    return json.dumps(item)
