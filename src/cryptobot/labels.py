"""Labelled outcomes: each eligible token's features at decision time and what happened.

The paper trader's shadow `labels` variants buy every token that passes the conviction
gates the moment it qualifies, with each exit in v1.LABEL_EXITS, the full fill model and
no wallet limit (v1.labels_universe). Clustered funding does not block a label, so every
filter can be measured. Their closed trades are the outcomes; the `labels` universe's gates
log holds the token's features at that moment, with the holder lookup's checks. This
module joins the two across every paper and backtest run into one table, the training
and test data for a learned scorer, and reports which filters and thesis signals
separate profitable tokens from the rest.

The narrative verdict (score, theme, red flags) comes from the run's lookups: it usually
arrives after the gate opens, so a rule that uses it has to wait for it, as the
narrative-gated universe does.

Usage: uv run python -m cryptobot.labels
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

from cryptobot.m6_report import Z95, wilson
from cryptobot.paper import read_table
from cryptobot.tracked import DERIVED, PAPER

LABELS_PATH = DERIVED / "labels.parquet"
LABELS_PREFIX = "labels|"
# Numeric features from the gates log (features.TokenState.snapshot) and conviction parts.
SNAPSHOT = (
    "age_s",
    "mcap_multiple",
    "progress",
    "dev_share",
    "insider_share",
    "insiders",
    "pro_traders",
    "largest_holder_share",
    "launch_run_mult",
    "trades",
    "sells",
    "early_buyer_share",
    "trades_per_progress_pct",
    "creator_graduates",
)
FLAGS = ("copycat", "creator_recent_launch", "has_telegram", "has_website")
X_KINDS = ("community", "tweet", "profile", "none")
MIN_SPLIT = 10  # fewer tokens than this on either side of a split: not shown
MIN_ROW = 10  # groups smaller than this show their count only
# Exit the analysis tables use when a run has several label exits: the best scalp of
# the 11 Oct backtest. Every exit's totals are listed first.
PRIMARY_EXIT = "tp5|sl10"
# Columns only live runs fill (backtests have no lookups); created empty when missing.
LIVE_ONLY = {
    "x_kind": pl.Utf8,
    "holders_differ": pl.Int64,
    "funding_ok": pl.Int64,
    "has_telegram": pl.Int64,
    "has_website": pl.Int64,
    "tracked_holders": pl.Int64,
    "narrative_score": pl.Int64,
    "narrative_theme": pl.Utf8,
    "narrative_flags": pl.Int64,
}


def _flag(value: object) -> int | None:
    return None if value is None else int(bool(value))


def _flatten(features: str) -> dict:
    f = json.loads(features)
    conv = f.get("conviction") or {}
    row: dict = {k: f.get(k) for k in SNAPSHOT}
    row |= {k: _flag(f.get(k)) for k in FLAGS}
    row["score"] = conv.get("score")
    row |= {f"pts_{k}": v for k, v in (conv.get("reasons") or {}).items()}
    kind = f.get("x_kind")
    row["x_kind"] = kind
    row |= {f"x_{k}": int(kind == k) for k in X_KINDS}
    checks = f.get("lookup_checks") or {}
    row["holders_differ"] = _flag(checks.get("top5_sol_balances_differ"))
    row["funding_ok"] = _flag(checks.get("top5_funding_not_clustered"))
    row["tracked_holders"] = f.get("tracked_holders")
    return row


def verdicts(events: Path) -> pl.DataFrame | None:
    """mint -> the narrative judge's last verdict: score, theme and number of red flags."""
    table = read_table(events, "lookups") if events.exists() else None
    if table is None:
        return None
    rows = table.filter(pl.col("result").fill_null("").str.contains('"narrative_update": true'))
    if rows.is_empty():
        return None
    result = pl.col("result")
    flags = result.str.json_path_match("$.narrative.red_flags").fill_null("[]")
    return (
        rows.sort("received_ms")
        .select(
            "mint",
            narrative_score=result.str.json_path_match("$.narrative.score").cast(
                pl.Int64, strict=False
            ),
            narrative_theme=result.str.json_path_match("$.narrative.theme"),
            narrative_flags=flags.str.count_matches('"') // 2,
        )
        .unique("mint", keep="last")
    )


def run_labels(run: Path) -> pl.DataFrame | None:
    """One run's labelled tokens, or None if it has no closed label trades."""
    trades_dir, gates_dir = run / "trades", run / "gates"
    if not trades_dir.exists() or not gates_dir.exists():
        return None
    trades = (
        pl.scan_parquet(sorted(trades_dir.glob("*/*.parquet")))
        .filter(pl.col("variant").str.starts_with(LABELS_PREFIX))
        .select(
            "variant", "mint", "entry_decided_ts_ms", "pnl_lamports", "net_return", "exit_reason"
        )
        .collect()
    )
    if trades.is_empty():
        return None
    gates = (
        pl.scan_parquet(sorted(gates_dir.glob("*/*.parquet")))
        .filter((pl.col("universe") == "labels") & pl.col("passed"))
        .select("mint", "ts_ms", "features")
        .collect()
    )
    if gates.is_empty():
        return None
    # Decode each gate row once (a run with several label exits has a trade per exit).
    flat = pl.DataFrame([_flatten(f) for f in gates["features"]], infer_schema_length=None)
    gates = pl.concat([gates.drop("features"), flat], how="horizontal").sort("ts_ms")
    out = (
        trades.sort("entry_decided_ts_ms")
        .join_asof(
            gates, left_on="entry_decided_ts_ms", right_on="ts_ms", by="mint", strategy="backward"
        )
        .filter(pl.col("ts_ms").is_not_null())
        .drop("ts_ms")
    )
    if out.is_empty():
        return None
    out = out.with_columns(
        run=pl.lit(run.name),
        win=pl.col("pnl_lamports") > 0,
        exit=pl.col("variant").str.strip_prefix(LABELS_PREFIX).str.replace(r"\|d\d+s$", ""),
    )
    found = verdicts(run / "events")
    if found is not None:
        out = out.join(found, on="mint", how="left")
    return out


BACKTEST = PAPER.parent / "backtest"  # backtest.BACKTEST, without importing the engine


def build(roots: tuple[Path, ...] = (PAPER, BACKTEST)) -> pl.DataFrame | None:
    """Labels from every run under `roots`. `source` says which: live paper runs have
    lookups; backtest runs scored offline (conviction.OFFLINE), so their holder and X
    features are not comparable and a scorer should treat the two separately."""
    frames = []
    for root in roots:
        runs = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
        frames += [
            df.with_columns(source=pl.lit(root.name))
            for r in runs
            if (df := run_labels(r)) is not None
        ]
    if not frames:
        return None
    # Overlapping backtests repeat the same label trade (same token, exit and decision).
    df = pl.concat(frames, how="diagonal_relaxed").unique(
        ["mint", "exit", "entry_decided_ts_ms"], keep="first", maintain_order=True
    )
    missing = {c: pl.lit(None, t) for c, t in LIVE_ONLY.items() if c not in df.columns}
    return df.with_columns(**missing).sort("entry_decided_ts_ms")


def split_table(df: pl.DataFrame) -> list[str]:
    """Each numeric feature split at its median: win rate and expectancy on each side."""
    lines = [
        "| Feature | Below median: n, win rate, expectancy | At or above: n, win rate, "
        "expectancy |",
        "|---|---|---|",
    ]
    skip = {"pnl_lamports", "net_return", "entry_decided_ts_ms", "win"} | {
        f"x_{k}" for k in X_KINDS
    }
    for col in df.columns:
        if col in skip or not df[col].dtype.is_numeric():
            continue
        sub = df.filter(pl.col(col).is_not_null())
        if sub.height < 2 * MIN_SPLIT:
            continue
        median = sub[col].median()
        lo, hi = sub.filter(pl.col(col) < median), sub.filter(pl.col(col) >= median)
        if lo.height < MIN_SPLIT or hi.height < MIN_SPLIT:
            continue
        lines.append(f"| {col} (median {median:g}) | {_side(lo)} | {_side(hi)} |")
    return lines


def _side(df: pl.DataFrame) -> str:
    n, wins = df.height, int(df["win"].sum())
    lo, hi = wilson(wins, n)
    return f"{n}, {wins / n:.1%} [{lo:.1%}-{hi:.1%}], {df['net_return'].mean():.1%}"


TABLE_HEAD = [
    "| | Tokens | Win rate [95% CI] | Mean net return [95% CI] | First half | Second half |",
    "|---|---|---|---|---|---|",
]


def _row(label: str, df: pl.DataFrame, split_ms: float) -> str:
    """One table row: count, win rate, mean net return, and the mean in each half of the
    period (a filter that only works in one half is probably luck)."""
    n = df.height
    if n < MIN_ROW:
        return f"| {label} | {n} | n/a | n/a | n/a | n/a |"
    wins = int(df["win"].sum())
    lo, hi = wilson(wins, n)
    mean, sd = df["net_return"].mean(), df["net_return"].std() or 0.0
    half = Z95 * sd / n**0.5
    first = df.filter(pl.col("entry_decided_ts_ms") < split_ms)["net_return"].mean()
    second = df.filter(pl.col("entry_decided_ts_ms") >= split_ms)["net_return"].mean()

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.1%}"

    return (
        f"| {label} | {n:,} | {wins / n:.1%} [{lo:.1%}-{hi:.1%}] "
        f"| {pct(mean)} [{pct(mean - half)} to {pct(mean + half)}] | {pct(first)} "
        f"| {pct(second)} |"
    )


def _looked_up(col: str) -> pl.Expr:
    """Passes, or the check was not looked up (backtests): a live-only filter in a stack."""
    return pl.col(col).fill_null(1) == 1


# Stage-one filters (CLAUDE.md), each added to the ones above it.
FILTER_STACK: tuple[tuple[str, pl.Expr], ...] = (
    ("not a copycat", pl.col("copycat") == 0),
    ("no bundled launch candle", pl.col("pts_no_bundle_candle") > 0),
    ("dev holds at most 5%", pl.col("dev_share") <= 0.05),
    ("insiders hold at most 20%", pl.col("insider_share") <= 0.20),
    ("largest holder at most 4%", pl.col("largest_holder_share") <= 0.04),
    ("first 10 buyers hold at most 15%", pl.col("early_buyer_share") <= 0.15),
    ("top-5 SOL balances differ (live)", _looked_up("holders_differ")),
    ("top-5 funding not clustered (live)", _looked_up("funding_ok")),
)
# Thesis and quality signals, each on its own: tokens with it against tokens without it.
SIGNALS: tuple[tuple[str, pl.Expr], ...] = (
    ("X link is a community", pl.col("x_kind") == "community"),
    ("X link is a tweet", pl.col("x_kind") == "tweet"),
    ("X link is a profile", pl.col("x_kind") == "profile"),
    ("no X link", pl.col("x_kind") == "none"),
    ("has a Telegram link", pl.col("has_telegram") == 1),
    ("has a website", pl.col("has_website") == 1),
    ("top-5 SOL balances differ", pl.col("holders_differ") == 1),
    ("top-5 funding not clustered", pl.col("funding_ok") == 1),
    ("narrative has no red flags", pl.col("narrative_flags") == 0),
    ("not a copycat", pl.col("copycat") == 0),
    ("creator had an earlier graduate", pl.col("creator_graduates") > 0),
    # Recent launches are seen in-run too, so empty means none was seen: "no".
    ("creator launched in the last 7 days", pl.col("creator_recent_launch").fill_null(0) == 1),
    ("no tracked wallets among holders", pl.col("tracked_holders") == 0),
)
# A "legit thesis", every part of it from the owner's list and V1: an X community, a
# narrative the judge rates 4-5 with no red flags, an original, and clean top-5 holders.
LEGIT_THESIS = (
    (pl.col("x_kind") == "community")
    & (pl.col("narrative_score") >= 4)
    & (pl.col("narrative_flags") == 0)
    & (pl.col("copycat") == 0)
    & (pl.col("holders_differ") == 1)
    & (pl.col("funding_ok") == 1)
)
AGES = (
    [10, 30, 60, 120, 300, 600],
    ["0-10 s", "10-30 s", "30-60 s", "1-2 min", "2-5 min", "5-10 min", "10-40 min"],
)


def report(df: pl.DataFrame) -> list[str]:
    exits = df["exit"].unique().sort().to_list()
    primary = PRIMARY_EXIT if PRIMARY_EXIT in exits else exits[0]
    split = df["entry_decided_ts_ms"].median()
    counts = df["source"].value_counts().sort("source").rows()
    sources = ", ".join(f"{s}: {c:,}" for s, c in counts)
    d = df.filter(pl.col("exit") == primary)
    live = d.filter(pl.col("x_kind").is_not_null())
    lines = [
        "# Labelled outcomes",
        "",
        "Generated by `uv run python -m cryptobot.labels`. Every token that passed the "
        "conviction gates, bought the moment it qualified with the paper fill model, fees "
        "included and network costs not. Paper trading and backtests only.",
        "",
        f"- Label trades: {df.height:,} from {df['run'].n_unique()} run(s) ({sources})",
        f"- Tables below use the `{primary}` exit; live-only signals use the {live.height:,} "
        "tokens with a holder lookup (paper runs).",
        "- First and second half split the period at its middle trade. A filter that helps "
        "in only one half is probably luck. Below "
        f"{MIN_ROW} tokens a row shows its count only.",
        "",
        "## Exits",
        "",
        *TABLE_HEAD,
        *(_row(e, df.filter(pl.col("exit") == e), split) for e in exits),
        "",
        "## Filters, each added to the ones above",
        "",
        *TABLE_HEAD,
        _row("every eligible token", d, split),
    ]
    cond = pl.lit(True)
    for label, expr in FILTER_STACK:
        cond = cond & expr
        lines.append(_row(f"+ {label}", d.filter(cond), split))
    lines += [
        "",
        "## Thesis and quality signals, one at a time",
        "",
        "Live-only signals (X link, links, holders, narrative, tracked wallets) count only "
        "tokens that had a lookup; the rest count every token.",
        "",
        *TABLE_HEAD,
    ]
    for label, expr in SIGNALS:
        lines.append(_row(f"{label}: yes", d.filter(expr), split))
        lines.append(_row(f"{label}: no", d.filter(~expr), split))
    lines += ["", "## Narrative score", "", *TABLE_HEAD]
    for score in (5, 4, 3, 2, 1):
        lines.append(_row(f"{score}/5", live.filter(pl.col("narrative_score") == score), split))
    lines.append(_row("not judged", live.filter(pl.col("narrative_score").is_null()), split))
    lines += [
        "",
        "## Legit thesis",
        "",
        "X community, narrative 4-5 with no red flags, not a copycat, top-5 balances differ "
        "and funding not clustered, against every other token that had a lookup.",
        "",
        *TABLE_HEAD,
        _row("legit thesis", live.filter(LEGIT_THESIS), split),
        _row("everything else", live.filter(~LEGIT_THESIS.fill_null(False)), split),
        "",
        "## Age at entry",
        "",
        *TABLE_HEAD,
    ]
    breaks, names = AGES
    aged = d.with_columns(age=pl.col("age_s").cut(breaks, labels=names))
    for name in names:
        lines.append(_row(name, aged.filter(pl.col("age") == name), split))
    lines += [
        "",
        "## Each feature split at its median",
        "",
        "A first look, one feature at a time and in sample; overlapping intervals mean no "
        f"clear difference. Features with fewer than {MIN_SPLIT} tokens on a side are left "
        "out.",
        "",
        *split_table(d),
        "",
    ]
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=LABELS_PATH)
    args = parser.parse_args(argv)
    df = build()
    if df is None:
        print("labels: no closed label trades in any paper run yet", file=sys.stderr)
        raise SystemExit(1)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.rename(args.out)
    print("\n".join(report(df)))


if __name__ == "__main__":
    main()
