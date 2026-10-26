"""Reddit through its official Data API, for social.py. Off until the owner adds keys.

Setup (free, non-commercial): create a "script" app at https://www.reddit.com/prefs/apps,
then put these lines in the server's `.env` (never in chat or code):

    REDDIT_CLIENT_ID=...
    REDDIT_CLIENT_SECRET=...
    REDDIT_USERNAME=...        # only for the User-Agent Reddit asks for

The app signs in with application-only OAuth (client credentials), so no Reddit password
is needed and it can only read. Reddit allows about 100 requests a minute; this makes
about one every 2 minutes.

What it reads:
- the newest posts in memecoin subreddits (REDDIT_SUBS, overridable with
  CRYPTOBOT_REDDIT_SUBS): mint addresses, $TICKERs, and words that spike above their own
  baseline (social.Spikes);
- r/popular's hottest titles every 10 minutes: words that spike there are general
  internet trends a coin might be named after.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
REDDIT_SUBS = (
    "memecoins",
    "CryptoMoonShots",
    "SolanaMemeCoins",
    "solana",
    "pumpfun",
    "CryptoCurrency",
)


def configured() -> bool:
    return bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"))


class Reddit:
    def __init__(self) -> None:
        self.client_id = os.environ["REDDIT_CLIENT_ID"]
        self.secret = os.environ["REDDIT_CLIENT_SECRET"]
        user = os.environ.get("REDDIT_USERNAME", "unknown")
        self.user_agent = f"linux:crypto-bot-research:0.1 (by /u/{user})"
        self.subs = subreddits()
        self.token: str | None = None
        self.expires = 0.0

    def _auth(self) -> str:
        if self.token is None or time.time() > self.expires - 60:
            basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(
                TOKEN_URL,
                data=body,
                headers={"Authorization": f"Basic {basic}", "User-Agent": self.user_agent},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                got = json.load(resp)
            self.token = got["access_token"]
            self.expires = time.time() + float(got.get("expires_in", 3600))
        return self.token

    def get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{API}{path}",
            headers={"Authorization": f"bearer {self._auth()}", "User-Agent": self.user_agent},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:  # token revoked early: get a new one on the next call
                self.token = None
            raise

    def new_posts(self) -> list[str]:
        """Title and text of the newest 100 posts across the memecoin subreddits."""
        return texts(self.get(f"/r/{'+'.join(self.subs)}/new?limit=100&raw_json=1"))

    def popular_titles(self) -> list[str]:
        return texts(self.get("/r/popular/hot?limit=100&raw_json=1"), body=False)


def texts(listing: dict, body: bool = True) -> list[str]:
    out = []
    for child in listing.get("data", {}).get("children", []):
        d = child.get("data", {})
        out.append(f"{d.get('title', '')} {d.get('selftext', '') if body else ''}")
    return out


# Keyless fallback: Reddit's public RSS feeds --------------------------------------------
#
# Reddit publishes an Atom feed for every listing, readable without an account. With no
# app keys in `.env`, social.py reads these instead, gently (every 5 and 15 minutes) and
# backing off when Reddit answers 429. No login, no bot-detection workaround: if Reddit
# stops serving the feed, the source simply goes quiet until keys are added.

RSS = "https://www.reddit.com"
ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
CONTENT = re.compile(r"<content[^>]*>(.*?)</content>", re.S)
TAG = re.compile(r"<[^>]+>")


def rss_user_agent() -> str:
    return f"linux:crypto-bot-research:0.1 (by /u/{os.environ.get('REDDIT_USERNAME', 'unknown')})"


def rss_texts(raw: bytes, body: bool = True) -> list[str]:
    """Title (and text) of each entry in a Reddit Atom feed, read as text (no XML parser)."""
    out = []
    for entry in ENTRY.findall(raw.decode("utf-8", "replace")):
        t = TITLE.search(entry)
        c = CONTENT.search(entry) if body else None
        text = html.unescape(t.group(1)) if t else ""
        if c:
            text += " " + TAG.sub(" ", html.unescape(html.unescape(c.group(1))))
        out.append(text)
    return out


def _rss(path: str) -> bytes:
    req = urllib.request.Request(f"{RSS}{path}", headers={"User-Agent": rss_user_agent()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def subreddits() -> tuple[str, ...]:
    """CRYPTOBOT_REDDIT_SUBS (comma separated, spaces and "r/" allowed) or the defaults."""
    raw = os.environ.get("CRYPTOBOT_REDDIT_SUBS", "")
    subs = tuple(s.strip().removeprefix("r/") for s in raw.split(",") if s.strip())
    return subs or REDDIT_SUBS


def rss_new_posts() -> list[str]:
    return rss_texts(_rss(f"/r/{'+'.join(subreddits())}/new/.rss?limit=100"))


def rss_popular_titles() -> list[str]:
    return rss_texts(_rss("/r/popular/hot/.rss?limit=100"), body=False)
