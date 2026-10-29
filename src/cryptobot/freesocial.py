"""More free social sources for social.py: Mastodon, GDELT news, YouTube and Farcaster.

All are official APIs and free. Mastodon and GDELT need no key; YouTube and Farcaster
need a free key in the server's `.env` and stay off without one:

    YOUTUBE_API_KEY=...     # Google Cloud console, YouTube Data API v3 (10,000 units/day)
    NEYNAR_API_KEY=...      # neynar.com free plan (Farcaster)

- **Mastodon** (mastodon.social's public trends API): trending hashtags become keywords;
  trending posts are counted for mints and $TICKERs.
- **GDELT** (DOC 2.0 API, open news data): for each watched coin with a distinctive name
  (6+ letters), the share of world news coverage mentioning it over 24 hours. GDELT asks
  for at most one request every 5 seconds; this waits 6.
- **YouTube** (Data API v3): the 50 most popular US videos every 30 minutes (1 unit each),
  whose title words feed a spike detector; and for watched coins, the number of videos
  posted in 24 hours that match the name or ticker (100 units each, so capped by
  YOUTUBE_SEARCHES_PER_DAY, default 60).
- **Farcaster** (Neynar's trending feed, free plan): posts counted for mints and $TICKERs.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from cryptobot.text import MIN_KEY_CHARS, norm

USER_AGENT = (
    "crypto-bot-research/0.1 (private research; github.com/envyllcompanies-lang/crypto-bot)"
)
MASTODON = "https://mastodon.social/api/v1/trends"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_GAP_S = 6.0
YOUTUBE = "https://www.googleapis.com/youtube/v3"
NEYNAR = "https://api.neynar.com/v2/farcaster/feed/trending"
TAG = re.compile(r"<[^>]+>")

Rows = list[tuple[str, str, float, str]]


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read()


# Mastodon ----------------------------------------------------------------------------


def mastodon_tags(raw: bytes) -> Rows:
    """Trending hashtags, ranked, as keywords (#FrustratedCartoons -> frustratedcartoons)."""
    rows = []
    for rank, tag in enumerate(json.loads(raw), 1):
        k = norm(tag.get("name"))
        if len(k) >= MIN_KEY_CHARS:
            rows.append(("mastodon_tag", k, float(rank), tag.get("name") or ""))
    return rows


SPAN = re.compile(r"</?span[^>]*>")


def mastodon_texts(raw: bytes) -> list[str]:
    """Post texts. Mastodon splits a long link's text into visible and hidden spans, so
    spans are dropped without a space (or a mint in a pump.fun link would be cut in two)
    before the other tags become spaces."""
    return [
        html.unescape(TAG.sub(" ", SPAN.sub("", s.get("content") or ""))) for s in json.loads(raw)
    ]


# GDELT -------------------------------------------------------------------------------


def gdelt_query(name: str | None) -> str | None:
    """A quoted phrase for the coin's name, or None if it is too short to mean much."""
    clean = re.sub(r"[^A-Za-z0-9 ]", " ", name or "").strip()
    clean = re.sub(r"\s+", " ", clean)
    return f'"{clean}"' if len(norm(clean)) >= 6 else None


def gdelt_volume(raw: bytes) -> dict | None:
    """Total and last-hour coverage share (% of monitored articles) over the timespan."""
    try:
        got = json.loads(raw)
    except ValueError:
        return None  # GDELT answers rate-limit notices as plain text
    points = (got.get("timeline") or [{}])[0].get("data", [])
    values = [float(p.get("value", 0)) for p in points]
    return {"gdelt_vol_24h": sum(values), "gdelt_vol_last": values[-1] if values else 0.0}


class Gdelt:
    def __init__(self) -> None:
        self.last = 0.0

    def ready(self) -> bool:
        return time.time() - self.last >= GDELT_GAP_S

    def volume(self, name: str | None) -> dict | None:
        q = gdelt_query(name)
        if q is None:
            return None
        self.last = time.time()
        params = urllib.parse.urlencode(
            {"query": q, "mode": "timelinevol", "format": "json", "timespan": "24h"}
        )
        return gdelt_volume(_get(f"{GDELT}?{params}"))


# YouTube -----------------------------------------------------------------------------


def youtube_configured() -> bool:
    return bool(os.environ.get("YOUTUBE_API_KEY"))


class YouTube:
    def __init__(self) -> None:
        self.key = os.environ["YOUTUBE_API_KEY"]
        self.per_day = int(os.environ.get("YOUTUBE_SEARCHES_PER_DAY", "60"))
        self.day = ""
        self.searches = 0

    def popular_titles(self) -> list[str]:
        params = urllib.parse.urlencode(
            {"part": "snippet", "chart": "mostPopular", "regionCode": "US",
             "maxResults": 50, "key": self.key}
        )  # fmt: skip
        got = json.loads(_get(f"{YOUTUBE}/videos?{params}"))
        return [it.get("snippet", {}).get("title", "") for it in got.get("items", [])]

    def can_search(self) -> bool:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.searches = today, 0
        return self.searches < self.per_day

    def videos_24h(self, name: str | None, symbol: str | None) -> dict | None:
        """Videos posted in the last 24 hours matching the coin's name or ticker."""
        terms = [t for t in (gdelt_query(name), symbol and f'"{symbol} crypto"') if t]
        if not terms or not self.can_search():
            return None
        self.searches += 1
        since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = urllib.parse.urlencode(
            {"part": "snippet", "q": "|".join(terms), "type": "video",
             "publishedAfter": since, "maxResults": 25, "key": self.key}
        )  # fmt: skip
        got = json.loads(_get(f"{YOUTUBE}/search?{params}"))
        return {"yt_videos_24h": float(len(got.get("items", [])))}


# Farcaster ---------------------------------------------------------------------------


def neynar_configured() -> bool:
    return bool(os.environ.get("NEYNAR_API_KEY"))


def farcaster_texts() -> list[str]:
    raw = _get(f"{NEYNAR}?limit=10", {"x-api-key": os.environ["NEYNAR_API_KEY"]})
    return [c.get("text") or "" for c in json.loads(raw).get("casts", [])]


# Lemmy -------------------------------------------------------------------------------
#
# An open, federated Reddit-like network with a free API and no key. Smaller than
# Reddit, but crypto communities exist (lemmy.world/c/cryptocurrency).

LEMMY = "https://lemmy.world/api/v3/post/list"
LEMMY_COMMUNITIES = ("cryptocurrency",)


def lemmy_texts(community: str | None = None, sort: str = "New", limit: int = 50) -> list[str]:
    params = {"sort": sort, "limit": limit}
    if community:
        params["community_name"] = community
    got = json.loads(_get(f"{LEMMY}?{urllib.parse.urlencode(params)}"))
    return [
        f"{p['post'].get('name', '')} {p['post'].get('body') or ''}" for p in got.get("posts", [])
    ]


# DexScreener per coin ---------------------------------------------------------------
#
# The free token endpoint (no key, about 300 requests a minute) returns a coin's pairs
# with the social links and websites registered for it on DexScreener and its paid
# boosts. A coin someone bothered (and paid) to fill in there is a coin with a team or
# community behind it.

DEX_TOKEN = "https://api.dexscreener.com/tokens/v1/solana/"


def dex_token(mint: str) -> dict | None:
    got = json.loads(_get(DEX_TOKEN + mint))
    if not got:
        return None
    pair = got[0]
    info = pair.get("info") or {}
    return {
        "dex_socials": float(len(info.get("socials") or [])),
        "dex_websites": float(len(info.get("websites") or [])),
        "dex_boosts_active": float((pair.get("boosts") or {}).get("active", 0)),
    }
