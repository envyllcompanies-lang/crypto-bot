"""X (Twitter) post counts through the official API, for social.py. Off by default: X has
no free tier for new developers, so this spends money and only runs within a monthly
budget the owner sets.

Setup, once the owner has decided to pay for X API access:

    X_BEARER_TOKEN=...              # from developer.x.com, in the server's .env only
    X_MONTHLY_BUDGET_USD=10         # hard cap; 0 or unset = off
    X_COST_PER_REQUEST_USD=0.005    # set to X's current price for counts/recent

Price as reported on 15 Oct 2026 by third-party guides (X's own pricing page could not be
read from here): about $0.005 per counts request and $0.005 per post read. The cap is
counted here, request by request, in data/derived/x_budget.json; check X's billing page
too.

What it asks: one `GET /2/tweets/counts/recent` per watched coin, the hourly number of
posts over the last 24 hours that mention its mint address, its $TICKER or (for names of
6+ letters) its name. Counts cost one request whatever the volume; reading posts costs
per post, so this never reads posts. Watched coins are the ones the paper trader puts on
its watchlist (late-curve and elite candidates; paper.Engine.watch_path).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptobot.text import norm

COUNTS = "https://api.x.com/2/tweets/counts/recent"


def configured() -> bool:
    try:
        budget = float(os.environ.get("X_MONTHLY_BUDGET_USD", "0"))
    except ValueError:
        budget = 0.0
    return bool(os.environ.get("X_BEARER_TOKEN")) and budget > 0


def query(mint: str, name: str | None, symbol: str | None) -> str:
    """X search query for one coin: its mint, cashtag and (long) name."""
    parts = [mint]
    if symbol and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,11}", symbol.strip().lstrip("$")):
        parts.append("$" + symbol.strip().lstrip("$"))
    clean = re.sub(r"[^A-Za-z0-9 ]", "", name or "").strip()
    if len(norm(clean)) >= 6:
        parts.append(f'"{clean}"')
    return " OR ".join(parts)


class Budget:
    """Requests made this calendar month (UTC), kept on disk so restarts don't reset it."""

    def __init__(self, path: Path, monthly_usd: float, cost_usd: float) -> None:
        self.path, self.monthly_usd, self.cost_usd = path, monthly_usd, cost_usd

    def _load(self) -> dict:
        month = datetime.now(UTC).strftime("%Y-%m")
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        except ValueError:  # unreadable (a crash mid-write): if it was written this
            # month, assume the month is spent rather than start again from 0
            written = datetime.fromtimestamp(self.path.stat().st_mtime, UTC).strftime("%Y-%m")
            data = {"month": month, "requests": math.inf} if written == month else {}
        return data if data.get("month") == month else {"month": month, "requests": 0}

    def spent(self) -> float:
        return self._load()["requests"] * self.cost_usd

    def take(self) -> bool:
        """Count one request if it fits in the budget; False if it would exceed it."""
        data = self._load()
        if (data["requests"] + 1) * self.cost_usd > self.monthly_usd:
            return False
        data["requests"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")  # a crash mid-write must not reset the count
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)
        return True


class XCounts:
    def __init__(self, budget_path: Path) -> None:
        self.token = os.environ["X_BEARER_TOKEN"]
        self.budget = Budget(
            budget_path,
            float(os.environ.get("X_MONTHLY_BUDGET_USD", "0")),
            float(os.environ.get("X_COST_PER_REQUEST_USD", "0.005")),
        )

    def counts(self, mint: str, name: str | None, symbol: str | None) -> dict | None:
        """{'x_posts_1h': n, 'x_posts_24h': n}, or None when the budget is used up."""
        if not self.budget.take():
            return None
        start = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = urllib.parse.urlencode(
            {"query": query(mint, name, symbol), "granularity": "hour", "start_time": start}
        )
        req = urllib.request.Request(
            f"{COUNTS}?{params}", headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            got = json.load(resp)
        return summarize(got)


def summarize(got: dict, now: datetime | None = None) -> dict | None:
    """Posts in the last complete hour and over 24 hours; None for an answer without
    counts (an error object is not "no posts")."""
    buckets = got.get("data")
    if not isinstance(buckets, list):
        return None
    now = now or datetime.now(UTC)
    total = got.get("meta", {}).get("total_tweet_count")
    if total is None:
        total = sum(b.get("tweet_count", 0) for b in buckets)
    # X's hourly buckets end on the hour, so the newest one is the unfinished current
    # hour: use the newest bucket that has ended (or the newest, if no end is given).
    done = [b for b in buckets if _ended(b, now)]
    last = (done or buckets)[-1].get("tweet_count", 0) if buckets else 0
    return {"x_posts_1h": float(last), "x_posts_24h": float(total)}


def _ended(bucket: dict, now: datetime) -> bool:
    """A bucket that covers a whole hour and is over (the current one ends "now")."""
    try:
        start, end = (
            datetime.fromisoformat(str(bucket[k]).replace("Z", "+00:00")) for k in ("start", "end")
        )
    except (KeyError, ValueError):
        return False
    return end <= now and (end - start).total_seconds() >= 3600
