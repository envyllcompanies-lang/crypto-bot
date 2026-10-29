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
from urllib.parse import urlparse

from cryptobot.holders import fetch_metadata

USER_AGENT = (
    "Mozilla/5.0 (compatible; crypto-bot-research/0.1; +https://github.com/envyllcompanies-lang)"
)
TGWEB_GAP_S = 5.0
TGWEB_BACKOFF_S = 600.0  # after an error (429, block, timeout): leave t.me alone for 10 min
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
        self.resume_at = 0.0
        self.seen: dict[str, set[str]] = {}  # channel -> post ids already counted

    def ready(self) -> bool:
        now = time.time()
        return now - self.last >= TGWEB_GAP_S and now >= self.resume_at

    def back_off(self) -> None:
        self.resume_at = time.time() + TGWEB_BACKOFF_S

    def _get(self, url: str) -> tuple[str, str]:
        """The page and the URL it ended up at after redirects."""
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(2_000_000).decode("utf-8", "replace"), resp.geturl()

    def _preview(self, handle: str) -> list[tuple[str, str, float]] | None:
        """Posts on a channel's public preview, or None when there isn't one: groups,
        users, bots and deleted names redirect away from /s/, and a page with no posts
        can't be told from a changed layout. Either way there is nothing to count."""
        page, final = self._get(f"https://t.me/s/{handle}")
        if not urlparse(final).path.startswith("/s/"):
            return None
        found = posts(page)
        return found or None

    def coin(self, uri: str | None) -> dict | None:
        """Members and recent posts of the Telegram a coin's metadata links to."""
        meta = fetch_metadata(uri or "")
        handle = handle_of((meta or {}).get("telegram"))
        if handle is None:
            return None
        out: dict = {}
        n = members(self._get(f"https://t.me/{handle}")[0])
        if n is not None:
            out["tg_members"] = float(n)
        time.sleep(TGWEB_GAP_S)
        preview = self._preview(handle)
        if preview is not None:
            out["tg_posts_1h"] = float(sum(1 for p in preview if p[2] > time.time() - 3600))
        return out or None

    def new_posts(self, channel: str) -> list[str]:
        """Texts of posts in a public channel not seen on earlier visits."""
        preview = self._preview(channel)
        if preview is None:  # not a public channel (or nothing readable): count nothing
            return []
        seen = self.seen.setdefault(channel, set())
        first_visit = not seen
        out = []
        for post_id, text, _ in preview:
            if post_id not in seen:
                seen.add(post_id)
                if not first_visit:  # the first visit only sets the baseline
                    out.append(text)
        if len(seen) > 500:  # keep the newest 200 by post number ("c/999" < "c/1000")
            self.seen[channel] = set(sorted(seen, key=_post_number)[-200:])
        return out


def _post_number(post_id: str) -> int:
    tail = post_id.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else -1


def channels() -> list[str]:
    raw = os.environ.get("TELEGRAM_WEB_CHANNELS", "")
    return [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]


def dumps(item: dict) -> str:
    return json.dumps(item)
