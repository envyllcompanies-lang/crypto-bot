"""Live social and attention signals, from free official sources, for the paper trader.

The owner's view is that attention is the whole game, and V3 says the same. X and
TikTok have no free API, so this collects what can be had for $0 without scraping a
website:

- **DexScreener** token boosts and profiles (`api.dexscreener.com`, free, no key, about
  60 requests a minute). A boost or a profile is paid promotion, so it is attention
  someone bought. Matched by mint address, so there are no false matches.
- **GeckoTerminal** trending Solana pools (`api.geckoterminal.com`, free, 30 a minute).
  Matched by mint address.
- **Google Trends** daily trending searches (the official RSS feed, US). Matched when a
  name or ticker equals the whole search phrase, normalised as in attention.py.
- **Wikipedia** yesterday's top 1,000 English articles (attention.wiki_top). Matched by
  name or ticker. On the history, coins that matched graduated 6.8% of the time against
  3.1% (reports/2026-10-14-attention-wikipedia.md).
- **DexScreener community takeovers** (a community took over an abandoned coin), by mint.
- **CoinGecko** top-searched coins (`/search/trending`, free, no key), by name or ticker.
- **4chan /biz/** catalog (the official read-only API, once a minute): counts of mint
  addresses and $TICKERs in thread openers and their latest replies, and `biz_spike` for
  words mentioned at least four times their own day-long baseline (`Spikes`).
- **Bluesky** posts (Jetstream, free, no login): mint addresses and $TICKERs counted
  over the last hour.

X, Telegram call channels and Pump.fun's own comments are left out: X is paid or
scraped against its terms, Telegram's user API risks the account and now bans scraping,
and Pump.fun's frontend API is undocumented (reports/2026-10-14-social-sources.md).

`run` polls the sources, saves every snapshot under data/live/social/<source>/<date>/,
and keeps a compact current state in data/derived/social_now.json. The paper trader
loads that file (paper.SocialFeed, recorded so replays see the same state) and stamps
each candidate's gate record with `signals()`. As CLAUDE.md requires for social data,
nothing trades on these yet: `report` measures whether they predict outcomes first.

Usage: uv run python -m cryptobot.social run      (on the server: crypto-bot-social.service)
       uv run python -m cryptobot.social report   (outcomes by signal, from paper runs)
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from cryptobot.attention import MIN_KEY_CHARS, keys, norm, wiki_top
from cryptobot.recorder import LIVE

SOCIAL = LIVE / "social"
NOW_PATH = LIVE.parent / "derived" / "social_now.json"
USER_AGENT = (
    "crypto-bot-research/0.1 (private research; github.com/envyllcompanies-lang/crypto-bot)"
)
SOURCES = {
    # name: (url, seconds between polls)
    "dex_boosts": ("https://api.dexscreener.com/token-boosts/latest/v1", 120),
    "dex_profiles": ("https://api.dexscreener.com/token-profiles/latest/v1", 120),
    "gt_trending": ("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", 300),
    "google_trends": ("https://trends.google.com/trending/rss?geo=US", 900),
    "dex_takeovers": ("https://api.dexscreener.com/community-takeovers/latest/v1", 300),
    "cg_trending": ("https://api.coingecko.com/api/v3/search/trending", 600),
    # 4chan's official read-only API asks for at most one request a second; once a minute.
    "biz": ("https://a.4cdn.org/biz/catalog.json", 60),
}
MINT_SOURCES = (
    "dex_boosts",
    "dex_profiles",
    "dex_takeovers",
    "gt_trending",
    "biz_mint",
    "bsky_mint",
)
JETSTREAM = "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"
BSKY_EVERY_S = 300
BSKY_WINDOW_MS = 3600 * 1000  # Bluesky mention counts cover the last hour
WIKI_EVERY_S = 6 * 3600
KEEP_MS = 6 * 3600 * 1000  # a signal counts for six hours after it was last seen
TICK_S = 10


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# Parsing: each source to rows of (kind, key, value, text) --------------------------


def parse_dex(raw: bytes, kind: str) -> list[tuple[str, str, float, str]]:
    rows = []
    for item in json.loads(raw):
        if item.get("chainId") != "solana" or not item.get("tokenAddress"):
            continue
        value = float(item.get("totalAmount") or item.get("amount") or 1)
        rows.append((kind, item["tokenAddress"], value, item.get("description") or ""))
    return rows


def parse_gt(raw: bytes) -> list[tuple[str, str, float, str]]:
    rows = []
    for rank, pool in enumerate(json.loads(raw).get("data", []), 1):
        base = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        if base.startswith("solana_"):
            name = pool.get("attributes", {}).get("name", "")
            rows.append(("gt_trending", base.removeprefix("solana_"), float(rank), name))
    return rows


ITEM = re.compile(r"<item>(.*?)</item>", re.S)
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
TRAFFIC = re.compile(r"<ht:approx_traffic>(.*?)</ht:approx_traffic>", re.S)


def parse_google(raw: bytes) -> list[tuple[str, str, float, str]]:
    """Titles and traffic from the RSS feed, read as text (no XML parser, so no entity
    expansion from a feed we don't control)."""
    rows = []
    text = raw.decode("utf-8", "replace")
    for rank, item in enumerate(ITEM.findall(text), 1):
        title_m, traffic_m = TITLE.search(item), TRAFFIC.search(item)
        title = title_m.group(1).strip() if title_m else ""
        traffic = traffic_m.group(1).strip() if traffic_m else ""
        key = norm(title)  # the whole search phrase: its last word is often generic
        if len(key) >= MIN_KEY_CHARS:
            rows.append(("google_trends", key, float(rank), f"{title} ({traffic})"))
    return rows


def parse_cg(raw: bytes) -> list[tuple[str, str, float, str]]:
    """CoinGecko's top-searched coins: name and ticker as keywords, by rank."""
    rows = []
    for rank, c in enumerate(json.loads(raw).get("coins", []), 1):
        item = c.get("item", {})
        for k in {norm(item.get("name")), norm(item.get("symbol"))}:
            if len(k) >= MIN_KEY_CHARS:
                rows.append(("cg_trending", k, float(rank), item.get("name") or ""))
    return rows


BASE58_MINT = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{2,11})\b")
TAG = re.compile(r"<[^>]+>")


def mentions(text: str) -> tuple[list[str], list[str]]:
    """Mint addresses and $TICKER keywords mentioned in a piece of text."""
    mints = BASE58_MINT.findall(text)
    tickers = [norm(t) for t in CASHTAG.findall(text)]
    return mints, [t for t in tickers if len(t) >= MIN_KEY_CHARS]


WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,19}")
BIZ_MAX_SHARE = 0.05  # a word in more than 5% of posts is generic, not a coin's name


class Spikes:
    """Flags words mentioned far more than their own baseline (an exponentially weighted
    average over about a day of polls). Ordinary words sit near their baseline; a coin
    name that suddenly gets talked about jumps above it."""

    def __init__(self, kind: str, alpha: float = 1 / 1440, warmup: int = 60) -> None:
        self.kind, self.alpha, self.warmup = kind, alpha, warmup
        self.base: dict[str, float] = {}
        self.polls = 0

    def update(self, counts: dict[str, float], min_count: float = 3, ratio: float = 4.0):
        self.polls += 1
        rows = []
        for w in set(self.base) | set(counts):
            n = counts.get(w, 0.0)
            base = self.base.get(w, 0.0)
            if self.polls > self.warmup and n >= min_count and n >= ratio * (base + 0.5):
                rows.append((self.kind, w, n / (base + 0.5), ""))
            new = base + self.alpha * (n - base)
            if new < 0.01 and n == 0:
                self.base.pop(w, None)  # forget words that stopped appearing
            else:
                self.base[w] = new
        return rows


def parse_biz(raw: bytes) -> list[tuple[str, str, float, str]]:
    """/biz/ thread openers and the latest replies the catalog shows: counts of mint
    addresses and $TICKERs, and of distinctive words (`biz_word`: in at least two posts
    but at most 5% of them), since /biz/ mostly names coins in plain words."""
    counts: dict[tuple[str, str], int] = {}
    word_posts: dict[str, int] = {}
    n_posts = 0
    for page in json.loads(raw):
        for thread in page.get("threads", []):
            for post in [thread, *thread.get("last_replies", [])]:
                # <wbr> splits long words (mint addresses) in 4chan's HTML: drop it first.
                raw_text = f"{post.get('sub', '')} {post.get('com', '')}".replace("<wbr>", "")
                text = html.unescape(TAG.sub(" ", raw_text))
                n_posts += 1
                mints, tickers = mentions(text)
                for m in mints:
                    counts[("biz_mint", m)] = counts.get(("biz_mint", m), 0) + 1
                for t in tickers:
                    counts[("biz_ticker", t)] = counts.get(("biz_ticker", t), 0) + 1
                for w in {norm(w) for w in WORD.findall(text)}:
                    word_posts[w] = word_posts.get(w, 0) + 1
    limit = max(2, int(n_posts * BIZ_MAX_SHARE))
    for w, n in word_posts.items():
        if 2 <= n <= limit and len(w) >= MIN_KEY_CHARS:
            counts[("biz_word", w)] = n  # raw counts; run() keeps only spikes (Spikes)
    return [(kind, key, float(n), "") for (kind, key), n in counts.items()]


class BlueskyCounter:
    """Counts mint addresses and $TICKERs in every public Bluesky post (Jetstream, free,
    no login), keeping only matches, so memory stays small."""

    def __init__(self) -> None:
        import threading

        self.seen: list[tuple[int, str, str]] = []  # (ms, kind, key)
        self.posts = 0
        self.lock = threading.Lock()  # add() runs in the listener thread, rows() in the main

    def add(self, text: str, now_ms: int) -> None:
        mints, tickers = mentions(text)
        with self.lock:
            self.posts += 1
            self.seen += [(now_ms, "bsky_mint", m) for m in mints]
            self.seen += [(now_ms, "bsky_ticker", t) for t in tickers]

    def rows(self, now_ms: int) -> list[tuple[str, str, float, str]]:
        cut = now_ms - BSKY_WINDOW_MS
        with self.lock:
            self.seen = [s for s in self.seen if s[0] >= cut]
            seen = list(self.seen)
        counts: dict[tuple[str, str], int] = {}
        for _, kind, key in seen:
            counts[(kind, key)] = counts.get((kind, key), 0) + 1
        return [(kind, key, float(n), "") for (kind, key), n in counts.items()]


def bluesky_thread(counter: BlueskyCounter) -> None:
    """Keep a Jetstream connection open in the background; reconnect on any error."""
    import asyncio
    import threading

    import websockets

    async def listen() -> None:
        while True:
            try:
                async with websockets.connect(JETSTREAM, max_size=2**22) as ws:
                    async for msg in ws:
                        rec = json.loads(msg).get("commit", {}).get("record") or {}
                        if text := rec.get("text"):
                            counter.add(text, int(time.time() * 1000))
            except Exception as e:
                print(f"bluesky: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                await asyncio.sleep(30)

    threading.Thread(target=lambda: asyncio.run(listen()), daemon=True).start()


def parse_wiki(day) -> list[tuple[str, str, float, str]]:
    rows = []
    for article, rank in wiki_top(day).select("article", "rank").iter_rows():
        for key in keys(article):
            rows.append(("wiki_top", key, float(rank), article))
    return rows


# State -------------------------------------------------------------------------------


def update(state: dict, rows: list[tuple[str, str, float, str]], now_ms: int) -> dict:
    """Fold a snapshot into the state: mints and keywords -> {kind: [value, last seen]}."""
    for kind, key, value, _ in rows:
        bucket = "mints" if kind in MINT_SOURCES else "keywords"
        entry = state.setdefault(bucket, {}).setdefault(key, {})
        old = entry.get(kind)
        if kind == "dex_boosts":
            value = max(value, old[0]) if old else value  # totalAmount is cumulative
        elif kind in ("gt_trending", "google_trends", "wiki_top", "cg_trending") and old:
            value = min(value, old[0])  # best rank seen
        entry[kind] = [value, now_ms]
    cut = now_ms - KEEP_MS
    for bucket in ("mints", "keywords"):
        for key in list(state.get(bucket, {})):
            kinds = {k: v for k, v in state[bucket][key].items() if v[1] >= cut}
            if kinds:
                state[bucket][key] = kinds
            else:
                del state[bucket][key]
    state["updated_ms"] = now_ms
    return state


def signals(state: dict | None, mint: str, name: str | None, symbol: str | None) -> dict:
    """What the current state says about one coin (the paper trader logs this)."""
    if not state:
        return {"loaded": False}
    out: dict = {"loaded": True}
    for kind, (value, _) in state.get("mints", {}).get(mint, {}).items():
        out[kind] = value
    for k in {norm(name), norm(symbol)}:
        if len(k) >= MIN_KEY_CHARS:
            for kind, (value, _) in state.get("keywords", {}).get(k, {}).items():
                out[kind] = min(value, out.get(kind, value))
    out["any"] = len(out) > 1
    return out


def _save(source: str, rows: list[tuple[str, str, float, str]], now_ms: int) -> None:
    day = datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y-%m-%d")
    folder = SOCIAL / source / day
    folder.mkdir(parents=True, exist_ok=True)
    schema = {"kind": pl.String, "key": pl.String, "value": pl.Float64, "text": pl.String}
    pl.DataFrame(rows, schema=schema, orient="row").with_columns(
        received_ms=pl.lit(now_ms, pl.Int64)
    ).write_parquet(folder / f"{now_ms}.parquet")


def write_state(state: dict, path: Path = NOW_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(path)


PARSERS = {
    "dex_boosts": lambda raw: parse_dex(raw, "dex_boosts"),
    "dex_profiles": lambda raw: parse_dex(raw, "dex_profiles"),
    "dex_takeovers": lambda raw: parse_dex(raw, "dex_takeovers"),
    "gt_trending": parse_gt,
    "google_trends": parse_google,
    "cg_trending": parse_cg,
    "biz": parse_biz,
}


def run(path: Path = NOW_PATH) -> None:
    """Poll every source on its own schedule until stopped; a failed poll is retried."""
    state: dict = json.loads(path.read_text()) if path.exists() else {}
    due = {name: 0.0 for name in [*SOURCES, "wiki_top", "bluesky"]}
    counter = BlueskyCounter()
    bluesky_thread(counter)
    biz_spikes = Spikes("biz_spike")
    print("social collector started", flush=True)
    while True:
        now = time.time()
        changed = False
        for name, at in due.items():
            if now < at:
                continue
            every = {"wiki_top": WIKI_EVERY_S, "bluesky": BSKY_EVERY_S}.get(name)
            every = every or SOURCES[name][1]
            try:
                if name == "wiki_top":
                    rows = parse_wiki((datetime.now(UTC) - timedelta(days=1)).date())
                elif name == "bluesky":
                    rows = counter.rows(int(time.time() * 1000))
                else:
                    rows = PARSERS[name](_get(SOURCES[name][0]))
                if name == "biz":  # keep mints and tickers; words only when they spike
                    words = {k: v for kind, k, v, _ in rows if kind == "biz_word"}
                    rows = [r for r in rows if r[0] != "biz_word"] + biz_spikes.update(words)
            except Exception as e:  # network or format trouble: log, retry in a minute
                print(f"{name}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                due[name] = now + 60
                continue
            now_ms = int(time.time() * 1000)
            _save(name, rows, now_ms)
            update(state, rows, now_ms)
            changed = True
            due[name] = now + every
        if changed:
            write_state(state, path)
        time.sleep(TICK_S)


# Report ------------------------------------------------------------------------------


def report(root: Path) -> str:
    """Closed paper trades by the social signals logged at their gate (all runs)."""
    from cryptobot.m6_report import Z95, _pct

    trades, gates = [], []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        t = sorted((run_dir / "trades").glob("*/*.parquet"))
        g = sorted((run_dir / "gates").glob("*/*.parquet"))
        if t and g:
            trades.append(
                pl.scan_parquet(t).select("variant", "mint", "net_return", "entry_decided_ts_ms")
            )
            gates.append(  # only the social part of each record, so it fits on the server
                pl.scan_parquet(g)
                .filter(pl.col("passed"))
                .select(
                    "mint",
                    "universe",
                    "ts_ms",
                    social=pl.col("features").str.json_path_match("$.social"),
                )
                .drop_nulls("social")
            )
    lines = ["# Social signals and paper-trade outcomes", ""]
    if not trades:
        return "\n".join([*lines, "No paper runs with social signals yet.", ""])
    t = (
        pl.concat(trades)
        .collect()
        .with_columns(universe=pl.col("variant").str.split("|").list.first())
    )
    g = pl.concat(gates).collect(streaming=True)
    j = (
        t.sort("entry_decided_ts_ms")
        .join_asof(
            g.sort("ts_ms"),
            left_on="entry_decided_ts_ms",
            right_on="ts_ms",
            by=["mint", "universe"],
        )
        .drop_nulls("social")
    )
    kinds = [
        "any",
        "dex_boosts",
        "dex_profiles",
        "dex_takeovers",
        "gt_trending",
        "cg_trending",
        "google_trends",
        "wiki_top",
        "biz_mint",
        "biz_ticker",
        "biz_spike",
        "bsky_mint",
        "bsky_ticker",
    ]
    for k in kinds:
        j = j.with_columns(pl.col("social").str.json_path_match(f"$.{k}").is_not_null().alias(k))
    lines += [
        "Generated by `uv run python -m cryptobot.social report`. Each closed paper trade "
        "with the signals its token had when it passed the gate. Shadow and wallet "
        "variants pooled; small groups are noise.",
        "",
        "| Signal | Has it | Trades | Mean [95% CI] |",
        "|---|---|---|---|",
    ]
    for k in kinds:
        for has in (True, False):
            r = j.filter(pl.col(k) == has)["net_return"]
            if r.len() == 0:
                continue
            half = Z95 * (r.std() or 0) / r.len() ** 0.5
            lines.append(
                f"| {k} | {'yes' if has else 'no'} | {r.len():,} | {_pct(r.mean())} "
                f"[{_pct(r.mean() - half)} to {_pct(r.mean() + half)}] |"
            )
    return "\n".join([*lines, ""])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--root", type=Path, default=LIVE.parent / "paper")
    args = parser.parse_args(argv)
    if args.command == "run":
        run()
    else:
        print(report(args.root))


if __name__ == "__main__":
    main()
