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
}
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
        bucket = "mints" if kind in ("dex_boosts", "dex_profiles", "gt_trending") else "keywords"
        entry = state.setdefault(bucket, {}).setdefault(key, {})
        old = entry.get(kind)
        if kind == "dex_boosts":
            value = max(value, old[0]) if old else value  # totalAmount is cumulative
        elif kind in ("gt_trending", "google_trends", "wiki_top") and old:
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


def run(path: Path = NOW_PATH) -> None:
    """Poll every source on its own schedule until stopped; a failed poll is retried."""
    state: dict = json.loads(path.read_text()) if path.exists() else {}
    due = {name: 0.0 for name in [*SOURCES, "wiki_top"]}
    print("social collector started", flush=True)
    while True:
        now = time.time()
        changed = False
        for name, at in due.items():
            if now < at:
                continue
            every = WIKI_EVERY_S if name == "wiki_top" else SOURCES[name][1]
            try:
                if name == "wiki_top":
                    rows = parse_wiki((datetime.now(UTC) - timedelta(days=1)).date())
                else:
                    raw = _get(SOURCES[name][0])
                    rows = (
                        parse_dex(raw, name)
                        if name.startswith("dex_")
                        else parse_gt(raw)
                        if name == "gt_trending"
                        else parse_google(raw)
                    )
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
    kinds = ["any", "dex_boosts", "dex_profiles", "gt_trending", "google_trends", "wiki_top"]
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
