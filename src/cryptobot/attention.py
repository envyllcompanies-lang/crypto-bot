"""Attention signals: is a coin named after something people are paying attention to?

Memecoins are an attention bet (docs/research/source-videos.md, V3). X and TikTok have
no free API, but Wikipedia's official pageview API is free, needs no key and goes back
years, so it is the one attention signal we can test on the Slinky21 history:

- Each UTC day's top 1,000 English Wikipedia articles (`wiki_top`), cached under
  data/derived/attention/. Meta pages (Main Page, Special:, lists of deaths...) are
  dropped.
- A coin *matches* when its name or ticker, normalised (lower case, letters and digits
  only), equals a top article's title (without any "(...)" part) on its launch day or
  the day before, or equals that title's last word when the title has two or three
  words (a surname: "Christa Pike" -> "pike"). Single letters and very short names
  don't count (MIN_KEY_CHARS). The rule was fixed before looking at any outcome.

`report` compares matched and unmatched coins: graduation rate, how many reach 90%
progress, and the returns of the late-curve rules from the timing map with PumpSwap
fills (screen.py --timing --pool-fills).

Wikimedia asks API users for a descriptive User-Agent and a modest request rate; this
makes one request per day of data and caches it.

Usage: uv run python -m cryptobot.attention [--timing data/derived/timing_pool_d2000]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from cryptobot.screen import DISCOVERY_END, _classic_creates, _pct, _summary
from cryptobot.slinky import SLINKY_ROOT, files

ATTENTION = SLINKY_ROOT.parent.parent / "derived" / "attention"
WIKI_TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m:02d}/{d:02d}"
USER_AGENT = (
    "crypto-bot-research/0.1 (private research; github.com/envyllcompanies-lang/crypto-bot)"
)
MIN_KEY_CHARS = 4
META = re.compile(
    r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Category:|Template:|Talk:|User:)"
    r"|^(Deaths_in|List_of|\d{4}_in_)",
)


def norm(text: str | None) -> str:
    """Lower case, accents dropped, letters and digits only."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fetch(day: date, attempts: int = 8) -> list[dict]:
    url = WIKI_TOP.format(y=day.year, m=day.month, d=day.day)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["items"][0]["articles"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            time.sleep(5 * 2**attempt)  # 429: back off (5 s, 10 s, 20 s...)
    raise RuntimeError(f"Wikipedia top articles for {day} failed")


def wiki_top(day: date, cache: Path = ATTENTION, attempts: int = 8) -> pl.DataFrame:
    """That day's top articles (article, views, rank), meta pages dropped; cached."""
    path = cache / f"wiki_top_{day:%Y-%m-%d}.parquet"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        rows = _fetch(day, attempts)
        time.sleep(2)
        df = pl.DataFrame(rows, schema={"article": pl.String, "views": pl.Int64, "rank": pl.Int64})
        df.write_parquet(path)
    df = pl.read_parquet(path)
    return df.filter(~pl.col("article").str.contains(META.pattern))


def keys(article: str) -> set[str]:
    """Match keys of one article title: the whole title, and a 2-3 word title's last word."""
    title = re.sub(r"_\(.*\)$", "", article)
    words = [w for w in title.split("_") if w]
    out = {norm(title)}
    if 2 <= len(words) <= 3:
        out.add(norm(words[-1]))
    return {k for k in out if len(k) >= MIN_KEY_CHARS}


def day_keys(day: date) -> dict[str, int]:
    """Match key -> best rank among that day's top articles."""
    best: dict[str, int] = {}
    for article, rank in wiki_top(day).select("article", "rank").iter_rows():
        for k in keys(article):
            best[k] = min(rank, best.get(k, rank))
    return best


def matches(creates: pl.DataFrame) -> pl.DataFrame:
    """Per coin: whether its name or ticker matched a trending article, the best rank,
    the matched key, and `first`: no earlier coin in the data matched the same key in
    the same two-day window (V3's "is this coin the first of its kind?")."""
    days = sorted(set(creates["day"].to_list()))
    by_day = {d: day_keys(d) for d in sorted({x for d in days for x in (d, d - timedelta(1))})}
    out = []
    seen: dict[str, int] = {}  # key -> launch ms of the first coin that matched it
    ordered = creates.sort("created_ms").select("mint", "name", "symbol", "day", "created_ms")
    for mint, name, symbol, day, created in ordered.iter_rows():
        trending = {**by_day[day - timedelta(1)], **by_day[day]}
        hits = [
            (trending[k], k)
            for k in (norm(name), norm(symbol))
            if len(k) >= MIN_KEY_CHARS and k in trending
        ]
        if not hits:
            out.append((mint, False, None, None, None))
            continue
        rank, key = min(hits)
        first_ms = seen.get(key)
        first = first_ms is None or created - first_ms > 2 * 86_400_000
        if first:
            seen[key] = created
        out.append((mint, True, rank, key, first))
    schema = {
        "mint": pl.String,
        "wiki_match": pl.Boolean,
        "wiki_rank": pl.Int64,
        "wiki_key": pl.String,
        "wiki_first": pl.Boolean,
    }
    return pl.DataFrame(out, schema=schema, orient="row")


def launches(root: Path = SLINKY_ROOT) -> pl.DataFrame:
    """Classic launches with name, ticker and launch day."""
    frames = []
    for _, create_path, _ in files(root):
        c = _classic_creates(create_path)
        names = (
            pl.scan_parquet(create_path)
            .select("mint", name="token_name", symbol="symbol")
            .collect()
        )
        frames.append(c.join(names.unique("mint"), on="mint", how="left"))
    df = pl.concat(frames).unique("mint", keep="first")
    return df.with_columns(
        day=pl.from_epoch("created_ms", time_unit="ms").dt.date(),
    )


def with_graduation(launch: pl.DataFrame, grads: Path) -> pl.DataFrame:
    """Each coin with whether it graduated (pumpswap.graduates)."""
    graduated = pl.read_parquet(grads).select("mint").with_columns(graduated=pl.lit(True))
    return launch.join(graduated, on="mint", how="left").with_columns(
        pl.col("graduated").fill_null(False)
    )


def report(df: pl.DataFrame, trades: pl.DataFrame | None) -> str:
    first, last = df["day"].min(), df["day"].max()
    m = df.filter("wiki_match")
    lines = [
        "# Attention: coins named after a trending Wikipedia topic",
        "",
        "Generated by `uv run python -m cryptobot.attention`. Classic Pump.fun launches "
        f"{first} to {last} (Slinky21). A coin matches when its name or ticker equals the "
        "title (or a two- or three-word title's last word) of one of the top 1,000 English "
        "Wikipedia articles on its launch day or the day before (rule in attention.py, fixed "
        "before looking at outcomes).",
        "",
        "| Coins | Count | Graduated |",
        "|---|---|---|",
    ]
    for label, g in (("Match", m), ("No match", df.filter(~pl.col("wiki_match")))):
        lines.append(f"| {label} | {g.height:,} | {_pct(g['graduated'].mean())} |")
    top = m.filter(pl.col("wiki_rank") <= 100)
    lines.append(
        f"| Match, article in the top 100 | {top.height:,} | {_pct(top['graduated'].mean())} |"
    )
    for label, g in (
        ("Match, first coin on that topic", m.filter("wiki_first")),
        ("Match, a later copy", m.filter(~pl.col("wiki_first"))),
    ):
        lines.append(f"| {label} | {g.height:,} | {_pct(g['graduated'].mean())} |")
    if trades is not None and trades.height:
        cut = int(DISCOVERY_END.timestamp() * 1000)
        group = (
            pl.when(~pl.col("wiki_match"))
            .then(pl.lit("no match"))
            .when(pl.col("wiki_first"))
            .then(pl.lit("first"))
            .otherwise(pl.lit("copy"))
        )
        t = trades.join(
            df.select("mint", "wiki_match", "wiki_first"), on="mint", how="inner"
        ).with_columns(
            group=group,
            period=pl.when(pl.col("created_ms") < cut)
            .then(pl.lit("1-23 Sep"))
            .otherwise(pl.lit("24 Sep-8 Oct")),
        )
        s = _summary(t, ["entry", "exit", "group", "period"]).sort(
            "entry", "exit", "period", "group"
        )
        lines += [
            "",
            "## Timing-map rules by match (PumpSwap fills, direct fees, 2 s delay)",
            "",
            "| Entry | Hold | Period | Coins | Trades | Mean [95% CI] |",
            "|---|---|---|---|---|---|",
        ]
        for r in s.iter_rows(named=True):
            lines.append(
                f"| {r['entry']} | {r['exit']} | {r['period']} | {r['group']} "
                f"| {r['n']:,} | {_pct(r['mean'])} [{_pct(r['mean'] - r['half'])} to "
                f"{_pct(r['mean'] + r['half'])}] |"
            )
    lines += ["", "Matched examples (name, ticker, article rank):", ""]
    for name, symbol, rank in (
        m.sort("wiki_rank").select("name", "symbol", "wiki_rank").head(15).iter_rows()
    ):
        lines.append(f"- {name} ({symbol}): rank {rank}")
    return "\n".join([*lines, ""])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timing", type=Path, default=ATTENTION.parent / "timing_pool_d2000")
    parser.add_argument(
        "--grads", type=Path, default=ATTENTION.parent / "pumpswap" / "graduates.parquet"
    )
    args = parser.parse_args(argv)
    launch = launches()
    df = with_graduation(launch, args.grads).join(matches(launch), on="mint", how="left")
    trades = None
    if args.timing.exists():
        trades = (
            pl.scan_parquet(sorted(args.timing.glob("part-*.parquet")))
            .filter(
                (pl.col("venue") == "Direct to the program")
                & pl.col("entry").is_in(["p05", "p10", "p25", "p50", "p75", "p90"])
                & pl.col("exit").is_in(["hold1m", "hold5m", "hold15m"])
            )
            .select("mint", "entry", "exit", "created_ms", "net_return", "flag")
            .unique(["mint", "entry", "exit"])
            .collect()
        )
    print(report(df, trades))


if __name__ == "__main__":
    main()
