"""X read through the owner's own logged-in account (twscrape), for social.py.

Off until the owner signs in once, on the server, over SSH:

    uv run python -m cryptobot.xscrape login     # asks for the X username and two
                                                  # browser cookies (hidden input)
    uv run python -m cryptobot.xscrape status    # is the account still working?

Then crypto-bot-x.service (`xscrape run`) looks up each coin on the paper trader's
watchlist, newest first, at most X_SCRAPE_PER_DAY requests a day (default 300) and one
every X_SCRAPE_GAP_S seconds (default 20):

- one search for the mint address (the Latest tab, one page of up to 20 posts):
  `x_ca_posts`, `x_ca_posts_1h`, `x_ca_authors`, `x_ca_likes`, `x_ca_views`,
  `x_ca_max_followers`, and `x_ca_new_share`, the share of posting accounts under 30
  days old (fresh accounts shilling a contract address are a classic bot sign);
- one search for the $TICKER when it has 4+ characters (shorter ones collide with
  stocks and other coins): `x_tk_posts_1h`, `x_tk_authors`;
- the coin's own X link from its metadata: a community's members and moderators
  (`x_comm_*`, V1's community check); a post's likes, reposts, views, its author's
  followers and account age and the post's age (`x_post_*`); or a profile's followers,
  account age, post count and paid check mark (`x_prof_*`).

Each coin's numbers go as one JSON line to data/derived/x_scrape.jsonl; social.py folds
them into its state like any other per-coin source, and the paper trader logs them in
`features.social`. Nothing trades on them: `social.py report` tests them first.

Risks the owner accepted in chat (15-19 Oct 2026): scraping breaks X's terms of service,
and X can lock or suspend the account, so a spare account is safer than a main one.
How it reads: twscrape calls the same internal API the x.com web app uses and sends
the x-client-transaction-id header the web app sends (X refuses requests without it).
Nothing else is done to pass as a browser: curl_cffi is not installed and the plain
httpx backend is forced (no TLS impersonation), no proxies, one account, slow. When X
rate-limits the account it waits; when X locks or logs it out it stops and says so,
and never signs in again by itself. twscrape's own usage telemetry (PostHog) is off.

The cookies are as good as a password. The owner types them into `login` on the server
(never in chat, code, commits or logs); they live only in data/derived/x_accounts.db
(git-ignored, mode 600) and nothing here prints them.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import time
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptobot.holders import X_HOSTS
from cryptobot.recorder import LIVE

# Before twscrape is imported: no telemetry, plain httpx (no browser TLS fingerprint).
os.environ["TWS_TELEMETRY"] = "0"
os.environ["DO_NOT_TRACK"] = "1"
os.environ["TWS_HTTP_BACKEND"] = "httpx"

DERIVED = LIVE.parent / "derived"
ACCOUNTS_DB = DERIVED / "x_accounts.db"
RESULTS_PATH = DERIVED / "x_scrape.jsonl"
CAP_PATH = DERIVED / "x_scrape_cap.json"
PER_DAY = 300
GAP_S = 20.0
SEARCH_PAGE = 20  # one page of the Latest tab: one request
NEW_ACCOUNT_DAYS = 30
MIN_TICKER = 4
RATE_LIMIT_PAUSE_S = 900  # the account is rate-limited: try again in 15 minutes
ERROR_PAUSE_S = 60
DEAD_CHECK_S = 600  # account locked or logged out: check every 10 minutes for a new login
QUEUE_MAX = 200

KINDS = (
    "x_ca_posts",
    "x_ca_posts_1h",
    "x_ca_authors",
    "x_ca_likes",
    "x_ca_views",
    "x_ca_max_followers",
    "x_ca_new_share",
    "x_tk_posts_1h",
    "x_tk_authors",
    "x_comm_members",
    "x_comm_mods",
    "x_post_likes",
    "x_post_reposts",
    "x_post_views",
    "x_post_author_followers",
    "x_post_author_days",
    "x_post_age_h",
    "x_prof_followers",
    "x_prof_days",
    "x_prof_posts",
    "x_prof_blue",
)
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
NOT_PROFILES = {"i", "home", "search", "explore", "hashtag", "intent", "share", "messages"}


def configured(db: Path = ACCOUNTS_DB) -> bool:
    return db.exists() and db.stat().st_size > 0


# Pure parts: what the numbers mean (tested without X) -------------------------------


def _days(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds() / 86400


def post_metrics(posts: list, now: datetime) -> dict[str, float]:
    """Counts over one page of search results for the mint address."""
    authors = {p.user.id: p.user for p in posts}
    recent = [p for p in posts if (_days(now, p.date) or 0) * 24 <= 1]
    ages = [_days(now, u.created) for u in authors.values()]
    new = [a for a in ages if a is not None and a < NEW_ACCOUNT_DAYS]
    followers = [u.followersCount or 0 for u in authors.values()]
    return {
        "x_ca_posts": float(len(posts)),
        "x_ca_posts_1h": float(len(recent)),
        "x_ca_authors": float(len(authors)),
        "x_ca_likes": float(sum(p.likeCount or 0 for p in posts)),
        "x_ca_views": float(sum(p.viewCount or 0 for p in posts)),
        "x_ca_max_followers": float(max(followers, default=0)),
        "x_ca_new_share": len(new) / len(authors) if authors else 0.0,
    }


def ticker_metrics(posts: list, now: datetime) -> dict[str, float]:
    m = post_metrics(posts, now)
    return {"x_tk_posts_1h": m["x_ca_posts_1h"], "x_tk_authors": m["x_ca_authors"]}


def ticker_query(symbol: str | None) -> str | None:
    """`$TICKER` for tickers of 4+ letters and digits; None for short or odd ones."""
    clean = (symbol or "").strip().lstrip("$")
    if len(clean) < MIN_TICKER or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{3,11}", clean):
        return None
    return f"${clean}"


def link_target(url: object) -> tuple[str, str] | None:
    """("community", id), ("tweet", id) or ("profile", handle) for an X link, else None."""
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in X_HOSTS):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts[:2] == ["i", "communities"] and len(parts) >= 3 and parts[2].isdigit():
        return "community", parts[2]
    if len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit():
        return "tweet", parts[2]
    if len(parts) == 1 and HANDLE.match(parts[0]) and parts[0].lower() not in NOT_PROFILES:
        return "profile", parts[0]
    return None


def community_metrics(c) -> dict[str, float]:
    return {
        "x_comm_members": float(c.memberCount or 0),
        "x_comm_mods": float(c.moderatorCount or 0),
    }


def tweet_metrics(t, now: datetime) -> dict[str, float]:
    out = {
        "x_post_likes": float(t.likeCount or 0),
        "x_post_reposts": float(t.retweetCount or 0),
        "x_post_views": float(t.viewCount or 0),
        "x_post_author_followers": float(t.user.followersCount or 0),
        "x_post_age_h": (_days(now, t.date) or 0) * 24,
    }
    days = _days(now, t.user.created)
    if days is not None:
        out["x_post_author_days"] = days
    return out


def profile_metrics(u, now: datetime) -> dict[str, float]:
    out = {
        "x_prof_followers": float(u.followersCount or 0),
        "x_prof_posts": float(u.statusesCount or 0),
        "x_prof_blue": 1.0 if u.blue else 0.0,
    }
    days = _days(now, u.created)
    if days is not None:
        out["x_prof_days"] = days
    return out


class DailyCap:
    """Requests made today (UTC), kept on disk so restarts don't reset it."""

    def __init__(self, path: Path = CAP_PATH, per_day: int = PER_DAY) -> None:
        self.path, self.per_day = path, per_day

    def _load(self) -> dict:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        return data if data.get("day") == day else {"day": day, "requests": 0}

    def left(self) -> int:
        return max(0, self.per_day - self._load()["requests"])

    def take(self) -> bool:
        data = self._load()
        if data["requests"] >= self.per_day:
            return False
        data["requests"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        return True


# The account ------------------------------------------------------------------------


class AccountDead(Exception):
    """X locked, suspended or logged out the account: the owner has to sign in again."""


class RateLimited(Exception):
    pass


class XScraper:
    def __init__(
        self,
        db: Path = ACCOUNTS_DB,
        cap: DailyCap | None = None,
        gap_s: float | None = None,
        api=None,
    ) -> None:
        self.cap = cap or DailyCap(per_day=int(os.environ.get("X_SCRAPE_PER_DAY", PER_DAY)))
        self.gap_s = float(os.environ.get("X_SCRAPE_GAP_S", GAP_S)) if gap_s is None else gap_s
        self.loop = asyncio.new_event_loop()
        self.last = 0.0
        if api is None:
            from twscrape import API, set_log_level

            set_log_level("WARNING")
            api = API(str(db), raise_when_no_account=True)
        self.api = api

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _wait_gap(self) -> None:
        wait = self.last + self.gap_s - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def _call(self, make):
        """One request: waits its turn, counts it against the day, maps account errors."""
        from twscrape import NoAccountError

        if not self.cap.take():
            return None
        self._wait_gap()
        try:
            return self._run(make())
        except NoAccountError:
            if self.active():
                raise RateLimited() from None
            raise AccountDead(self.problem()) from None

    def active(self) -> bool:
        return any(a["active"] for a in self._run(self.api.pool.accounts_info()))

    def problem(self) -> str:
        info = self._run(self.api.pool.accounts_info())
        if not info:
            return "no account"
        return info[0].get("error_msg") or "inactive"

    async def _search_page(self, q: str) -> list:
        from twscrape.models import parse_tweets

        async with aclosing(self.api.search_raw(q, limit=SEARCH_PAGE)) as gen:
            async for rep in gen:  # the first page only: exactly one request
                return list(parse_tweets(rep.json(), SEARCH_PAGE))
        return []

    def search(self, q: str) -> list | None:
        return self._call(lambda: self._search_page(q))

    def lookup(self, target: tuple[str, str]):
        kind, ident = target
        if kind == "community":
            return self._call(lambda: self.api.community_info(int(ident)))
        if kind == "tweet":
            return self._call(lambda: self.api.tweet_details(int(ident)))
        return self._call(lambda: self.api.user_by_login(ident))

    def coin(self, item: dict, x_link: str | None, now: datetime | None = None) -> dict[str, float]:
        """Every number for one watched coin (fewer when the day's cap runs out)."""
        now = now or datetime.now(UTC)
        out: dict[str, float] = {}
        posts = self.search(f'"{item["mint"]}"')
        if posts is not None:
            out |= post_metrics(posts, now)
        q = ticker_query(item.get("symbol"))
        if q and (posts := self.search(q)) is not None:
            out |= ticker_metrics(posts, now)
        target = link_target(x_link)
        if target is not None and (got := self.lookup(target)) is not None:
            if target[0] == "community":
                out |= community_metrics(got)
            elif target[0] == "tweet":
                out |= tweet_metrics(got, now)
            else:
                out |= profile_metrics(got, now)
        return out


def x_link_of(uri: str | None) -> str | None:
    from cryptobot.holders import fetch_metadata

    try:
        meta = fetch_metadata(uri or "") or {}
    except Exception:  # a dead metadata link: search only
        return None
    link = meta.get("twitter")
    return link if isinstance(link, str) else None


def write_result(item: dict, values: dict[str, float], path: Path = RESULTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"mint": item["mint"], "ts_ms": int(time.time() * 1000), "values": values}
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


# Service and commands ---------------------------------------------------------------


def run() -> None:
    from cryptobot.watch import WATCH_PATH, Watchlist

    if not configured():
        print("x scrape: no account yet; run `python -m cryptobot.xscrape login`", flush=True)
    while not configured():
        time.sleep(DEAD_CHECK_S)
    x = XScraper()
    watch = Watchlist(WATCH_PATH)
    queue: list[dict] = []
    paused_until = 0.0
    print(f"x scrape started: {x.cap.per_day} requests a day, {x.gap_s:.0f} s apart", flush=True)
    while True:
        queue = (queue + watch.new())[-QUEUE_MAX:]
        if not queue or time.time() < paused_until or x.cap.left() == 0:
            time.sleep(5)
            continue
        item = queue.pop()  # newest first: a stale coin is worth less than a fresh one
        try:
            values = x.coin(item, x_link_of(item.get("uri")))
        except RateLimited:
            print("x scrape: rate-limited by X; pausing 15 minutes", flush=True)
            queue.append(item)
            paused_until = time.time() + RATE_LIMIT_PAUSE_S
            continue
        except AccountDead as e:
            print(
                f"x scrape: X stopped the account ({e}). Reading is off until the owner runs "
                "`python -m cryptobot.xscrape login` again.",
                file=sys.stderr,
                flush=True,
            )
            while not x.active():
                time.sleep(DEAD_CHECK_S)
            print("x scrape: account active again; resuming", flush=True)
            continue
        except Exception as e:  # network or format trouble: skip this coin, slow down
            print(f"x scrape: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            paused_until = time.time() + ERROR_PAUSE_S
            continue
        if values:
            write_result(item, values)


def _cookie_value(prompt: str) -> str:
    value = getpass.getpass(prompt).strip()
    if not value or re.search(r"[\s;=]", value):
        sys.exit("That doesn't look like a cookie value; nothing was saved.")
    return value


def login() -> None:
    """Save the owner's X session cookies to the accounts db (hidden input, never shown)."""
    from twscrape import API

    print(
        "Sign in to the X account to use in a browser (a spare account is safer).\n"
        "Then: x.com > F12 > Application (Chrome) or Storage (Firefox) > Cookies >\n"
        "https://x.com, and copy two values: auth_token and ct0. They work like a\n"
        "password: type them here only, never in chat. Don't log out of X in that\n"
        "browser afterwards, or the cookies stop working (close the tab instead).\n"
    )
    username = input("X username (without @): ").strip().lstrip("@")
    if not HANDLE.match(username):
        sys.exit("That isn't an X username; nothing was saved.")
    auth = _cookie_value("auth_token (hidden): ")
    ct0 = _cookie_value("ct0 (hidden): ")
    ACCOUNTS_DB.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)  # the db file is created readable by its owner only
    try:
        api = API(str(ACCOUNTS_DB), raise_when_no_account=True)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            api.pool.add_account_cookies(username, f"auth_token={auth}; ct0={ct0}")
        )
    finally:
        os.umask(old_umask)
    os.chmod(ACCOUNTS_DB, 0o600)
    print("Saved. Checking it works with one request...")
    x = XScraper(api=api, gap_s=0)
    try:
        user = x.lookup(("profile", username))
    except (AccountDead, RateLimited) as e:
        sys.exit(f"X didn't accept these cookies ({type(e).__name__}: {e}).")
    if user is None:
        sys.exit("X didn't return the profile; check the username and try again.")
    print(f"Working: @{user.username}, {user.followersCount:,} followers.")
    print("The X service picks it up within 10 minutes (or: sudo systemctl restart crypto-bot-x).")


def status() -> None:
    if not configured():
        print("No X account saved. Run: uv run python -m cryptobot.xscrape login")
        return
    x = XScraper(api=_api_only())
    for a in x._run(x.api.pool.accounts_info()):
        state = "active" if a["active"] else f"stopped ({a['error_msg']})"
        print(f"@{a['username']}: {state}; {a['total_req']} requests in all")
    print(f"today: {x.cap.per_day - x.cap.left()} of {x.cap.per_day} requests used")
    if RESULTS_PATH.exists():
        with RESULTS_PATH.open() as f:
            print(f"coins looked up (current file): {sum(1 for _ in f)}")


def _api_only():
    from twscrape import API

    return API(str(ACCOUNTS_DB), raise_when_no_account=True)


def logout() -> None:
    """Forget the saved cookies (the X session itself stays valid until logged out on X)."""
    if ACCOUNTS_DB.exists():
        ACCOUNTS_DB.unlink()
    print("Deleted the saved X session. Reading X is off.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run", "login", "status", "logout"))
    args = parser.parse_args(argv)
    {"run": run, "login": login, "status": status, "logout": logout}[args.command]()


if __name__ == "__main__":
    main()
