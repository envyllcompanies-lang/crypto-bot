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
import json
import os
import time
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
        subs = os.environ.get("CRYPTOBOT_REDDIT_SUBS")
        self.subs = tuple(subs.split(",")) if subs else REDDIT_SUBS
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

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
