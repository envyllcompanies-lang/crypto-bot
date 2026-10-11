"""Print the M6 paper-trading report for one run (default: the latest in data/paper/).

Run with `uv run python -m cryptobot.m6_report [RUN_DIR] > reports/<date>-m6-paper.md`.
Every number comes from the run's files through this module.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.curve import to_lamports
from cryptobot.features import BUNDLE_CANDLE_MULT, PRO_MIN_BUY_LAMPORTS
from cryptobot.paper import PAPER, read_table
from cryptobot.v1 import CORE, MAX_TRACKED_HOLDERS, NOT_EVALUATED, V1_LAUNCH_USD

LAG_HOURS_SHOWN = 48
SPOT_CHECKS = 25
TEXT_MAX_CHARS = 40  # creator-written names, cut to this
TIERS = ("very_high", "good", "average")
CONVICTION_REASON = re.compile(r"^conviction (\d+) (\w+)$")
MIN_TRADES = 30  # below this many closed trades a variant's numbers are anecdotes
NETWORK_LEVELS_SOL = (0.0, 0.0001, 0.0005, 0.001)  # priority fee + tip per transaction
Z95 = 1.959964


def latest_run(root: Path = PAPER) -> Path:
    runs = sorted(p for p in root.iterdir() if (p / "summary.json").exists())
    if not runs:
        raise SystemExit(f"no finished runs in {root}")
    return runs[-1]


def wilson(wins: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a win rate."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(centre - half, 0.0), min(centre + half, 1.0)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ts(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def lag_by_hour(events: Path) -> pl.DataFrame | None:
    """Seconds from each trade's block time to when we received it, per UTC hour."""
    files = sorted((events / "trade").glob("*/*.parquet"))
    if not files:
        return None
    lag = (pl.col("received_ms") - pl.col("timestamp") * 1000) / 1000
    return (
        pl.scan_parquet(files)
        .select(hour=pl.col("received_ms") // 3_600_000 * 3_600_000, lag=lag)
        .group_by("hour")
        .agg(
            trades=pl.len(),
            median=pl.col("lag").median(),
            p90=pl.col("lag").quantile(0.9),
            worst=pl.col("lag").max(),
        )
        .sort("hour")
        .collect()
    )


def lag_section(events: Path) -> list[str]:
    lines = [
        "## Feed lag",
        "",
        "Seconds from each trade's block time to when we received it. Block times are whole "
        "seconds, so each value is rough to about 1 second.",
        "",
    ]
    table = lag_by_hour(events) if events.exists() else None
    if table is None or table.is_empty():
        return [*lines, "No recorded events for this run.", ""]
    shown = table.tail(LAG_HOURS_SHOWN)
    if shown.height < table.height:
        lines += [f"Last {shown.height} of {table.height} hours shown.", ""]
    lines += ["| Hour (UTC) | Trades | Median | 90th percentile | Worst |", "|---|---|---|---|---|"]
    for hour, n, median, p90, worst in shown.iter_rows():
        start = datetime.fromtimestamp(hour / 1000, UTC).strftime("%Y-%m-%d %H:00")
        lines.append(f"| {start} | {n:,} | {median:.2f} s | {p90:.2f} s | {worst:.2f} s |")
    return [*lines, ""]


def lookup_delays(events: Path) -> pl.DataFrame | None:
    """Seconds from asking for a token's holder lookup to each answer, by kind.

    "holders" is the lookup entries wait for (holders, funders, metadata); "narrative" is
    the AI judge's later verdict, which no entry waits for. Both count from the request.
    """
    if not events.exists():
        return None
    table = read_table(events, "lookups")
    if table is None or table.is_empty():
        return None
    narrative = pl.col("result").fill_null("").str.contains('"narrative_update": true')
    return (
        table.select(
            kind=pl.when(narrative).then(pl.lit("narrative")).otherwise(pl.lit("holders")),
            failed=pl.col("error").is_not_null(),
            delay=(pl.col("received_ms") - pl.col("requested_ms")) / 1000,
        )
        .group_by("kind")
        .agg(
            n=pl.len(),
            failed=pl.col("failed").sum(),
            median=pl.col("delay").median(),
            p90=pl.col("delay").quantile(0.9),
            worst=pl.col("delay").max(),
        )
        .sort("kind")
    )


def lookup_section(events: Path) -> list[str]:
    lines = [
        "## Lookup delay",
        "",
        "Seconds from requesting a token's checks to the answer. Entries wait for the holder "
        "lookup only; the narrative verdict is logged later and gates nothing.",
        "",
    ]
    table = lookup_delays(events)
    if table is None:
        return [*lines, "No lookups in this run.", ""]
    lines += ["| Lookup | Answers | Failed | Median | 90th percentile | Worst |"]
    lines += ["|---|---|---|---|---|---|"]
    for kind, n, failed, median, p90, worst in table.iter_rows():
        lines.append(
            f"| {kind} | {n:,} | {failed:,} | {median:.2f} s | {p90:.2f} s | {worst:.2f} s |"
        )
    return [*lines, ""]


def core_name() -> str:
    drop, target, stop = (round(x * 100) for x in CORE)
    return f"dip{drop}|tp{target}|sl{stop}|d2s"


def _sol(lamports: int | None) -> str:
    return "n/a" if lamports is None else f"{lamports / to_lamports(1):.4f}"


def md_text(value: object, limit: int = TEXT_MAX_CHARS) -> str:
    """Untrusted creator text for a markdown table cell: one line, cut short, inside a code
    span so links, emphasis and HTML show as plain text. Pipes are escaped because GFM
    tables split cells on them even inside code spans."""
    if not isinstance(value, str) or not value.replace("`", "").strip():
        return "n/a"
    text = " ".join(value.replace("`", "").split())[:limit]
    return "`" + text.replace("\\", "/").replace("|", "\\|") + "`"


def md_code(value: object, limit: int = 120) -> str:
    """A link or mint as inline code, with no spaces; never a clickable link."""
    if not isinstance(value, str):
        return "n/a"
    return md_text("".join(value.split()), limit)


def md_cell(value: str) -> str:
    """Our own text (variant names) in a table cell: escape the pipes in it."""
    return value.replace("|", "\\|")


def variant_table(trades: pl.DataFrame) -> pl.DataFrame:
    win = pl.col("pnl_lamports") > 0
    return (
        trades.group_by("universe", "variant")
        .agg(
            n=pl.len(),
            wins=win.sum(),
            avg_win=pl.col("net_return").filter(win).mean(),
            avg_loss=pl.col("net_return").filter(~win).mean(),
            expectancy=pl.col("net_return").mean(),
            pnl=pl.col("pnl_lamports").sum(),
            flagged=(pl.col("flags") != "").sum(),
        )
        .sort(["universe", "expectancy"], descending=[False, True])
    )


RESULT_HEADER = [
    "| Universe | Variant | Trades | Win rate [95% CI] | Avg win | Avg loss | Expectancy "
    "| PnL (SOL) | Cash (SOL) | Open (SOL) | Equity (SOL) | Flagged |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
]


def result_rows(
    table: pl.DataFrame | None, summary: dict, names: list[tuple[str, str]] | None = None
) -> list[str]:
    """Markdown rows of variant results; `names` lists (universe, variant) to show, in
    order, including variants that closed no trades."""
    cash = summary.get("final_balance_lamports", {})
    open_ = summary.get("open_value_lamports", {})
    equity = summary.get("equity_lamports", {})
    rows = {} if table is None else {row[1]: row for row in table.iter_rows()}
    order = names if names is not None else [(row[0], name) for name, row in rows.items()]
    out = []
    for u, name in order:
        wallet = (
            f"| {_sol(cash.get(name))} | {_sol(open_.get(name, 0))} "
            f"| {_sol(equity.get(name, cash.get(name)))} "
        )
        short = md_cell(name.removeprefix(f"{u}|"))
        if name not in rows:
            out.append(f"| {u} | {short} | 0 | n/a | n/a | n/a | n/a | +0.0000 {wallet}| 0 |")
            continue
        _, _, n, wins, avg_win, avg_loss, exp, pnl, flagged = rows[name]
        lo, hi = wilson(wins, n)
        out.append(
            f"| {u} | {short} | {n} | {_pct(wins / n)} [{_pct(lo)}-{_pct(hi)}] "
            f"| {_pct(avg_win)} | {_pct(avg_loss)} | {_pct(exp)} "
            f"| {pnl / to_lamports(1):+.4f} {wallet}| {flagged} |"
        )
    return out


def headline_variants(variants: list[dict]) -> list[tuple[str, str]]:
    """V1's core rule in each V1 universe, then every conviction variant."""
    core = core_name()
    out = [
        (v["universe"], v["name"])
        for v in variants
        if v["kind"] in ("new_pairs", "final_stretch") and v["name"] == f"{v['universe']}|{core}"
    ]
    return out + [(v["universe"], v["name"]) for v in variants if v.get("conviction")]


def tier_section(trades: pl.DataFrame | None, conviction_names: set[str]) -> list[str]:
    lines = [
        "## Conviction by tier",
        "",
        "Closed trades of the conviction variants, by the tier the token had when the entry "
        "was decided. Win rate with a 95% Wilson interval; expectancy is the mean net return.",
        "",
    ]
    sub = None
    if trades is not None and conviction_names:
        sub = trades.filter(pl.col("variant").is_in(list(conviction_names)))
    if sub is None or sub.is_empty():
        return [*lines, "No conviction trades closed in this run.", ""]
    tier = pl.col("entry_reason").str.extract(r"^conviction \d+ (\w+)$")
    table = (
        sub.with_columns(tier=tier)
        .group_by("tier")
        .agg(n=pl.len(), wins=(pl.col("pnl_lamports") > 0).sum(), exp=pl.col("net_return").mean())
    )
    found = {t: (n, w, e) for t, n, w, e in table.iter_rows()}
    lines += ["| Tier | Trades | Win rate [95% CI] | Expectancy |", "|---|---|---|---|"]
    for t in TIERS:
        if t not in found:
            lines.append(f"| {t} | 0 | n/a | n/a |")
            continue
        n, wins, exp = found[t]
        lo, hi = wilson(wins, n)
        lines.append(f"| {t} | {n} | {_pct(wins / n)} [{_pct(lo)}-{_pct(hi)}] | {_pct(exp)} |")
    return [*lines, ""]


def _features_before(gates: pl.DataFrame | None, mint: str, ts: int, universe=None) -> dict:
    """The token's features from its last gates row at or before `ts`."""
    if gates is None:
        return {}
    rows = gates.filter((pl.col("mint") == mint) & (pl.col("ts_ms") <= ts))
    if universe is not None:
        rows = rows.filter(pl.col("universe") == universe)
    if rows.is_empty():
        return {}
    return json.loads(rows.sort("ts_ms")["features"][-1])


def spot_check_section(
    decisions: pl.DataFrame | None,
    trades: pl.DataFrame | None,
    gates: pl.DataFrame | None,
    names: list[tuple[str, str]],
) -> list[str]:
    lines = [
        "## Spot-check list",
        "",
        f"The last {SPOT_CHECKS} entries by the variants above, to check by hand in Axiom or "
        "Padre. Token names, symbols and X links are written by the token's creator: they are "
        "shown as text, never followed. A listing is not a recommendation.",
        "",
    ]
    wanted = {name for _, name in names}
    if decisions is None:
        return [*lines, "No entries in this run.", ""]
    fills = decisions.filter((pl.col("kind") == "fill_buy") & pl.col("variant").is_in(wanted))
    if fills.is_empty():
        return [*lines, "No entries in this run.", ""]
    fills = fills.sort("ts_ms").tail(SPOT_CHECKS).reverse()
    exits: dict[tuple[str, str, int], tuple[str, float]] = {}
    if trades is not None:
        for v, m, ts, why, ret in trades.select(
            "variant", "mint", "entry_ts_ms", "exit_reason", "net_return"
        ).iter_rows():
            exits[(v, m, ts)] = (why, ret)
    lines += [
        "| Entry (UTC) | Variant | Mint | Token | X link | Conviction | Narrative | Exit "
        "| Net return |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for ts, variant, mint, reason in fills.select("ts_ms", "variant", "mint", "reason").iter_rows():
        feats = _features_before(gates, mint, ts)
        token = f"{md_text(feats.get('name'))} ({md_text(feats.get('symbol'))})"
        x_link = md_code(feats.get("x_link")) if feats.get("x_kind") not in (None, "none") else ""
        x_cell = f"{feats.get('x_kind') or 'n/a'} {x_link}".strip()
        match = CONVICTION_REASON.match(reason or "")
        if match:
            conv = f"{match[1]} {match[2]}"
        else:
            c = _features_before(gates, mint, ts, "conviction").get("conviction") or {}
            conv = f"{c['score']} {c['tier']}" if "score" in c else "n/a"
        narrative = feats.get("narrative")
        story = (
            f"{narrative['score']}/5 {md_text(narrative.get('theme'), 60)}"
            if narrative
            else "not evaluated"
        )
        why, ret = exits.get((variant, mint, ts), ("open", None))
        lines.append(
            f"| {_ts(ts)} | {md_cell(variant)} | {md_code(mint)} | {token} | {x_cell} | {conv} "
            f"| {story} | {why} | {_pct(ret)} |"
        )
    return [*lines, ""]


def report(run: Path) -> list[str]:
    summary = json.loads((run / "summary.json").read_text())
    variants = json.loads((run / "variants.json").read_text())
    trades, gates = read_table(run, "trades"), read_table(run, "gates")
    decisions = read_table(run, "decisions")
    stats = summary["stats"]
    headline = headline_variants(variants)
    conviction_names = {v["name"] for v in variants if v.get("conviction")}
    lines = [
        f"# M6: paper trading, run {run.name}",
        "",
        "Generated by `uv run python -m cryptobot.m6_report`. Do not edit by hand.",
        "Paper trading only: simulated fills on live data, no transaction was sent.",
        "",
        "## Run",
        "",
        "| | |",
        "|---|---|",
        f"| Mode | {summary['mode']} |",
        f"| Status | {'still running' if summary.get('running') else 'finished'} |",
        f"| Source | {'/'.join(Path(summary['source']).parts[-3:])} |",
        f"| From | {_ts(summary['first_event_ms'])} |",
        f"| To | {_ts(summary['last_event_ms'])} |",
        f"| Launches seen | {stats.get('creates', 0):,} |",
        f"| Classic launches tracked | {stats.get('classic_creates', 0):,} |",
        f"| Trades on tracked tokens | {stats.get('trades_on_tracked_tokens', 0):,} |",
        f"| Tokens that graduated | {stats.get('graduated', 0):,} |",
        f"| Stream gaps | {stats.get('gaps', 0):,} |",
        f"| Tokens with a missed trade | {stats.get('tainted_missed_trade', 0):,} |",
        f"| Holder and X-link lookups done | {stats.get('lookups_done', 0):,} |",
        f"| Lookups that failed (checks not evaluated) | {stats.get('lookup_errors', 0):,} |",
        f"| Copycat launches (name or symbol seen in 24 h) | {stats.get('copycats', 0):,} |",
        f"| Variants | {summary['variants']} |",
        f"| Closed paper trades | {summary['closed_trades']:,} |",
        "",
        "## Funnel",
        "",
        "Distinct tokens that passed each universe's rules at least once:",
        "",
        "| Universe | Tokens |",
        "|---|---|",
    ]
    universes = sorted({v["universe"] for v in variants})
    passed: dict[str, int] = {}
    if gates is not None:
        counts = gates.filter(pl.col("passed")).group_by("universe").agg(pl.col("mint").n_unique())
        passed = dict(counts.iter_rows())
    lines += [f"| {u} | {passed.get(u, 0):,} |" for u in universes]
    lines += [""]

    has_trades = trades is not None and not trades.is_empty()
    table = variant_table(trades) if has_trades else None
    explain = [
        "Win rate with a 95% Wilson interval. Expectancy is the mean net return per closed "
        f"trade after fees, before network costs. Variants with fewer than {MIN_TRADES} trades "
        "are anecdotes. Cash is the paper wallet's SOL, Open what its open positions would "
        "fetch if sold into the current curve after fees, Equity the two together.",
        "",
    ]
    v1_lines = [
        "## V1 as stated",
        "",
        f"V1's core rule ({core_name()}: 40% dip, +45% target, exit at the first price below "
        "entry, 2 s delay) in each V1 universe, and the conviction variants (entry drop from "
        "the token's conviction tier, V1's exits).",
        "",
        *explain,
        *RESULT_HEADER,
        *result_rows(table, summary, headline),
        "",
        *tier_section(trades, conviction_names),
        *spot_check_section(decisions, trades, gates, headline),
    ]
    lines = lines[:5] + v1_lines + lines[5:]  # right after the title block
    lines += lag_section(Path(summary["source"]))
    lines += lookup_section(Path(summary["source"]))

    lines += ["## Experiments: every variant", ""]
    if table is None:
        return [*lines, "No paper trades closed in this run.", "", *caveats(0)]
    lines += [*explain, *RESULT_HEADER, *result_rows(table, summary)]
    idle = len(variants) - table.height
    lines += ["", f"{idle} of {len(variants)} variants closed no trades.", ""]

    core = core_name()
    lines += [
        f"## Network cost sensitivity ({core})",
        "",
        "Expectancy of V1's core rule if each transaction also paid this much in priority fee "
        "and tip:",
        "",
        "| Universe | " + " | ".join(f"{x:g} SOL/tx" for x in NETWORK_LEVELS_SOL) + " |",
        "|---|" + "---|" * len(NETWORK_LEVELS_SOL),
    ]
    for u in [v["universe"] for v in variants if v["name"] == f"{v['universe']}|{core}"]:
        sub = trades.filter(pl.col("variant") == f"{u}|{core}")
        cells = []
        for level in NETWORK_LEVELS_SOL:
            if sub.is_empty():
                cells.append("n/a")
                continue
            net = to_lamports(level)
            adj = (sub["pnl_lamports"] - 2 * net) / (sub["cost_lamports"] + net)
            cells.append(_pct(adj.mean()))
        lines.append(f"| {u} | " + " | ".join(cells) + " |")
    return [*lines, "", *caveats(trades.height)]


def caveats(n_trades: int) -> list[str]:
    pro_sol = PRO_MIN_BUY_LAMPORTS / to_lamports(1)
    return [
        "## Read this before the numbers",
        "",
        f"- {n_trades:,} closed paper trades in total, spread over many variants. Treat any "
        f"variant with fewer than {MIN_TRADES} trades as an anecdote. Positions still open in "
        "a running session are not counted until they close.",
        "- Fills are simulated: our orders land after the delay on the curve other traders "
        "left, with our own price impact, but nobody reacts to our trades.",
        "- V1's X-link and top-5 holder checks are looked up once per candidate; entries "
        "wait for the answer, and a failed lookup leaves those checks not evaluated. "
        f"'Few tracked wallets' allows at most {MAX_TRACKED_HOLDERS} holders from the "
        "tracked-wallet list (tracked.py, rebuilt nightly from recorded trades); until the "
        "first list exists it is not evaluated"
        + (". Not evaluated at all: " + ", ".join(NOT_EVALUATED) if NOT_EVALUATED else "")
        + ". Each variant starts with a 1 SOL wallet.",
        f"- Proxies: 'pro traders' are wallets that bought at least {pro_sol:g} SOL of the "
        f"token; a bundle launch candle is a run to {BUNDLE_CANDLE_MULT:g}x launch before any "
        f"sell; dollar levels assume V1's launch at ${V1_LAUNCH_USD:,}.",
        "- Flagged trades were closed by graduation or by the end of the run (valued by selling "
        "into the last curve), or overlapped a stream gap or a missed trade; their exit price "
        "is less certain.",
        "- Final Stretch takes no entries in a run's first 40 minutes, because the column's "
        "market-cap ranking needs every token up to 40 minutes old.",
        "- The funnel counts tokens whose rules passed at least once; the gates table logs only "
        "changes between pass and fail.",
        "- Conviction (conviction.py) is our own proposal on top of V1, not V1's rule. Its "
        "weights and tier cut-offs are first guesses to be tuned on these results. Its points "
        "come from structural signals with published evidence (copycat, creator record, "
        "early-buyer share, trades to climb the curve, dev and insider shares, holder "
        "checks). The LLM narrative score (Groq, then Gemini, free tiers) is shown but scores "
        "nothing: it is logged until it beats the on-chain signals on later data.",
        "- Copycats are counted against launches seen since the run started, so a run's first "
        "24 hours find fewer of them.",
        "",
    ]


def main(argv: list[str]) -> None:
    run = Path(argv[0]) if argv else latest_run()
    print("\n".join(report(run)))


if __name__ == "__main__":
    main(sys.argv[1:])
