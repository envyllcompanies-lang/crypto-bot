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


def scan_table(root: Path, table: str) -> pl.LazyFrame | None:
    """One table of a run folder, lazily, or None if it has no files."""
    files = sorted((root / table).glob("*/*.parquet"))
    return pl.scan_parquet(files) if files else None


def latest_run(root: Path = PAPER) -> Path:
    runs = sorted(p for p in root.iterdir() if (p / "summary.json").exists())
    if not runs:
        raise SystemExit(f"no finished runs in {root}")
    return runs[-1]


def clustered_half(values: pl.Series, clusters: pl.Series, z: float = Z95) -> float | None:
    """Half-width of a 95% interval on the mean when observations come in clusters (one
    token traded by several variants is one piece of evidence, not several): the
    cluster-robust standard error with the usual G/(G-1) correction. None with fewer
    than two observations or clusters, where the spread is unknown."""
    n, g = values.len(), clusters.n_unique()
    if n < 2 or g < 2:
        return None
    dev = pl.DataFrame({"c": clusters, "d": values - values.mean()})
    sums = dev.group_by("c").agg(pl.col("d").sum())["d"]
    var = float((sums**2).sum()) * g / (g - 1) / n**2
    return z * var**0.5


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
        "lookup, except in `conviction_narrative`, which also waits for the narrative verdict.",
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
    conv = [v for v in variants if v.get("conviction") and not v.get("shadow")]
    return out + [(v["universe"], v["name"]) for v in conv]


def _outcomes(df: pl.DataFrame, by: list[str]) -> dict[tuple, tuple[int, int, float]]:
    table = df.group_by(by).agg(
        n=pl.len(), wins=(pl.col("pnl_lamports") > 0).sum(), exp=pl.col("net_return").mean()
    )
    return {tuple(r[:-3]): tuple(r[-3:]) for r in table.iter_rows()}


def _outcome_cells(found: tuple[int, int, float] | None) -> str:
    if found is None:
        return "0 | n/a | n/a"
    n, wins, exp = found
    lo, hi = wilson(wins, n)
    return f"{n} | {_pct(wins / n)} [{_pct(lo)}-{_pct(hi)}] | {_pct(exp)}"


def tier_section(trades: pl.DataFrame | None, conviction_universes: dict[str, str]) -> list[str]:
    """`conviction_universes` maps each conviction variant's name to its universe."""
    lines = [
        "## Conviction by tier",
        "",
        "Closed trades of the conviction variants, by universe and by the tier the token had "
        "when the entry was decided. `conviction_narrative` caps each tier by the LLM "
        "narrative score; `conviction` ignores it. Win rate with a 95% Wilson interval; "
        "expectancy is the mean net return.",
        "",
    ]
    sub = None
    if trades is not None and conviction_universes:
        sub = trades.filter(pl.col("variant").is_in(list(conviction_universes)))
    if sub is None or sub.is_empty():
        return [*lines, "No conviction trades closed in this run.", ""]
    tier = pl.col("entry_reason").str.extract(r"^conviction \d+ (\w+)$")
    universe = pl.col("variant").replace_strict(conviction_universes, default=None)
    found = _outcomes(sub.with_columns(tier=tier, universe=universe), ["universe", "tier"])
    lines += ["| Universe | Tier | Trades | Win rate [95% CI] | Expectancy |"]
    lines += ["|---|---|---|---|---|"]
    for u in sorted(set(conviction_universes.values())):
        tiers = ("random",) if u == "control_random" else TIERS
        for t in ("elite",) if u.startswith("elite") else tiers:
            lines.append(f"| {u} | {t} | {_outcome_cells(found.get((u, t)))} |")
    return [*lines, ""]


BEST_SHOWN = 10


def best_section(trades: pl.DataFrame | None) -> list[str]:
    """Is any variant profitable? The best by mean net return, with a 95% interval on the
    mean (normal approximation), among variants with at least MIN_TRADES trades."""
    lines = [
        "## Is any variant profitable?",
        "",
        f"Variants with at least {MIN_TRADES} closed trades (shadow variants included: a "
        "per-trade mean does not depend on the wallet), best mean net return per "
        "trade first (after fees, before network costs). The interval is a 95% interval on "
        "the mean. With this many variants, the best is the luckiest as well as the best: "
        "only a lower bound above zero, confirmed on later data, would count.",
        "",
    ]
    if trades is None or trades.is_empty():
        return [*lines, "No closed trades.", ""]
    table = (
        trades.group_by("variant")
        .agg(n=pl.len(), exp=pl.col("net_return").mean(), sd=pl.col("net_return").std())
        .filter(pl.col("n") >= MIN_TRADES)
        .with_columns(half=Z95 * pl.col("sd") / pl.col("n").sqrt())
        .sort("exp", descending=True)
    )
    positive = table.filter(pl.col("exp") > 0).height
    proven = table.filter(pl.col("exp") - pl.col("half") > 0).height
    lines += [
        f"- Variants with at least {MIN_TRADES} trades: {table.height}",
        f"- With a positive mean: {positive}",
        f"- With the 95% lower bound above zero: {proven}",
        "",
        "| Variant | Trades | Mean net return [95% CI] |",
        "|---|---|---|",
    ]
    for name, n, exp, _, half in table.head(BEST_SHOWN).iter_rows():
        lines.append(
            f"| {md_cell(name)} | {n} | {_pct(exp)} [{_pct(exp - half)} to {_pct(exp + half)}] |"
        )
    return [*lines, ""]


def chance_section(trades: pl.DataFrame | None, variants: list[dict]) -> list[str]:
    """The luck test: the score's picks against a coin flip and against every token."""
    lines = [
        "## Picks vs chance",
        "",
        "Every conviction-kind universe with V1's exits (+45% target, sell at the first price "
        "below entry). `control_random` passes the same gates, then a coin flip on the mint "
        "picks half the tokens, bought on a 25% dip. `labels` (no wallet limit) buys every "
        "eligible token the moment it qualifies: the base rate. A universe whose picks don't "
        "beat `control_random` net of costs is not showing skill, only the market's mood.",
        "",
    ]
    names = {
        v["name"]: v["universe"]
        for v in variants
        if v.get("conviction") and v.get("target") == 0.45 and v.get("stop_tolerance") == 0.0
    }
    sub = None if trades is None else trades.filter(pl.col("variant").is_in(list(names)))
    if sub is None or sub.is_empty():
        return [*lines, "No conviction-kind trades closed in this run.", ""]
    found = _outcomes(sub, ["universe"])
    lines += ["| Universe | Trades | Win rate [95% CI] | Expectancy |", "|---|---|---|---|"]
    for u in sorted(set(names.values())):
        lines.append(f"| {u} | {_outcome_cells(found.get((u,)))} |")
    return [*lines, ""]


def narrative_scores(events: Path) -> pl.DataFrame | None:
    """mint -> the LLM narrative score from the run's lookups (last verdict per mint)."""
    table = read_table(events, "lookups") if events.exists() else None
    if table is None:
        return None
    verdicts = table.filter(pl.col("result").fill_null("").str.contains('"narrative_update": true'))
    if verdicts.is_empty():
        return None
    score = pl.col("result").str.json_path_match("$.narrative.score").cast(pl.Int64, strict=False)
    return verdicts.sort("received_ms").select("mint", narrative=score).unique("mint", keep="last")


def narrative_section(trades: pl.DataFrame | None, ungated: set[str], events: Path) -> list[str]:
    """Does the LLM narrative score predict outcomes? Ungated conviction trades only, so
    the score did not choose the trades it is judged on."""
    lines = [
        "## Conviction by narrative score",
        "",
        "Closed trades of the ungated `conviction` variants, by the LLM narrative score its "
        "token got (1-5; none = not judged). If higher scores don't win more here, the "
        "narrative gate isn't earning its delay.",
        "",
    ]
    scores = narrative_scores(events)
    sub = None
    if trades is not None and ungated:
        sub = trades.filter(pl.col("variant").is_in(list(ungated)))
    if sub is None or sub.is_empty() or scores is None:
        return [*lines, "No judged conviction trades in this run.", ""]
    found = _outcomes(sub.join(scores, on="mint", how="left"), ["narrative"])
    lines += ["| Narrative | Trades | Win rate [95% CI] | Expectancy |", "|---|---|---|---|"]
    for s in (5, 4, 3, 2, 1, None):
        lines.append(f"| {s or 'none'} | {_outcome_cells(found.get((s,)))} |")
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
    all_trades = read_table(run, "trades")
    stats = summary["stats"]
    headline = headline_variants(variants)
    # Decisions and gates can be millions of rows (a backtest): scan them and keep only
    # what the report shows, the spot-check entries and those tokens' gate rows.
    wanted = [name for _, name in headline]
    decisions = gates = None
    gates_lf = scan_table(run, "gates")
    if (dec_lf := scan_table(run, "decisions")) is not None:
        decisions = (
            dec_lf.filter((pl.col("kind") == "fill_buy") & pl.col("variant").is_in(wanted))
            .sort("ts_ms")
            .tail(SPOT_CHECKS)
            .collect()
        )
    if gates_lf is not None and decisions is not None:
        mints = decisions["mint"].unique().to_list()
        gates = gates_lf.filter(pl.col("mint").is_in(mints)).collect()
    # Shadow variants (labels) are not paper wallets: only "Picks vs chance" shows them.
    shadow = [v["name"] for v in variants if v.get("shadow")]
    trades = all_trades
    if all_trades is not None and shadow:
        trades = all_trades.filter(~pl.col("variant").is_in(shadow))
    conviction_universes = {
        v["name"]: v["universe"] for v in variants if v.get("conviction") and not v.get("shadow")
    }
    ungated = {n for n, u in conviction_universes.items() if u == "conviction"}
    backtest = summary["mode"] == "backtest"
    title = (
        f"# M3: backtest, run {run.name}" if backtest else f"# M6: paper trading, run {run.name}"
    )
    kind = (
        "Backtest only: simulated fills on historical data (Slinky21), no lookups, no "
        "transaction was sent."
        if backtest
        else "Paper trading only: simulated fills on live data, no transaction was sent."
    )
    if summary.get("parts"):
        kind += (
            f" Run in {summary['parts']} parts side by side, each starting with fresh 1 SOL "
            "wallets: Cash, Open and Equity add up the parts' changes, so they are not one "
            "wallet's balance and can go below zero. Judge variants by expectancy."
        )
    lines = [
        title,
        "",
        "Generated by `uv run python -m cryptobot.m6_report`. Do not edit by hand.",
        kind,
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
    if gates_lf is not None:
        counts = (
            gates_lf.filter(pl.col("passed"))
            .group_by("universe")
            .agg(pl.col("mint").n_unique())
            .collect()
        )
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
        *best_section(all_trades),
        *chance_section(all_trades, variants),
        *tier_section(trades, conviction_universes),
        *narrative_section(trades, ungated, Path(summary["source"])),
        *spot_check_section(decisions, trades, gates, headline),
    ]
    lines = lines[:5] + v1_lines + lines[5:]  # right after the title block
    lines += lag_section(Path(summary["source"]))
    lines += lookup_section(Path(summary["source"]))

    lines += ["## Experiments: every variant", ""]
    if table is None:
        return [*lines, "No paper trades closed in this run.", "", *caveats(0)]
    lines += [*explain, *RESULT_HEADER, *result_rows(table, summary)]
    wallets = len(variants) - len(shadow)
    idle = wallets - table.height
    lines += ["", f"{idle} of {wallets} wallet variants closed no trades.", ""]

    lines += [
        "## Network cost sensitivity (every variant that closed trades)",
        "",
        "The variants above pay no network costs. Expectancy per trade if each transaction "
        "(buy and sell) also paid this much in priority fee and tip:",
        "",
        "| Variant | Trades | " + " | ".join(f"{x:g} SOL/tx" for x in NETWORK_LEVELS_SOL) + " |",
        "|---|---|" + "---|" * len(NETWORK_LEVELS_SOL),
    ]
    for name in sorted(trades["variant"].unique().to_list()):
        sub = trades.filter(pl.col("variant") == name)
        cells = []
        for level in NETWORK_LEVELS_SOL:
            net = to_lamports(level)
            adj = (sub["pnl_lamports"] - 2 * net) / (sub["cost_lamports"] + net)
            cells.append(_pct(adj.mean()))
        lines.append(f"| {md_cell(name)} | {sub.height:,} | " + " | ".join(cells) + " |")
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
        "checks). The LLM narrative score (Groq, then Gemini, free tiers) adds no points. "
        "`conviction` ignores it; `conviction_narrative` uses it only to cap the tier "
        "(1-2 never buy, 3 at most average, 4 at most good, only 5 very high) and waits for "
        "the verdict before any entry. Comparing the two shows whether the gate helps.",
        "- Copycats are counted against launches seen since the run started, so a run's first "
        "24 hours find fewer of them.",
        "",
    ]


def main(argv: list[str]) -> None:
    run = Path(argv[0]) if argv else latest_run()
    print("\n".join(report(run)))


if __name__ == "__main__":
    main(sys.argv[1:])
