"""Labelled outcomes: each eligible token's features at decision time and what happened.

The paper trader's shadow `labels` variant buys every token that passes the conviction
gates the moment it qualifies, with V1's exits and the full fill model, and no wallet
limit (v1.labels_universe). Its closed trades are the outcomes; the `labels` universe's
gates log holds the token's features at that moment. This module joins the two across
every paper run into one table: the training and test data for a learned scorer, and a
first look at which features separate winners from losers.

The narrative score comes from the run's lookups (the judge's verdict usually arrives
after the gate opens), so a scorer that uses it has to wait for the verdict, as the
narrative-gated universe does.

Usage: uv run python -m cryptobot.labels
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

from cryptobot.m6_report import narrative_scores, wilson
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
FLAGS = ("copycat", "creator_recent_launch")
X_KINDS = ("community", "tweet", "profile", "none")
MIN_SPLIT = 10  # fewer tokens than this on either side of a split: not shown


def _flatten(features: str) -> dict:
    f = json.loads(features)
    conv = f.get("conviction") or {}
    row: dict = {k: f.get(k) for k in SNAPSHOT}
    row |= {k: None if f.get(k) is None else int(bool(f[k])) for k in FLAGS}
    row["score"] = conv.get("score")
    row |= {f"pts_{k}": v for k, v in (conv.get("reasons") or {}).items()}
    kind = f.get("x_kind")
    row |= {f"x_{k}": int(kind == k) for k in X_KINDS}
    return row


def run_labels(run: Path) -> pl.DataFrame | None:
    """One run's labelled tokens, or None if it has no closed label trades."""
    trades_dir, gates_dir = run / "trades", run / "gates"
    if not trades_dir.exists() or not gates_dir.exists():
        return None
    trades = (
        pl.scan_parquet(sorted(trades_dir.glob("*/*.parquet")))
        .filter(pl.col("variant").str.starts_with(LABELS_PREFIX))
        .select("mint", "entry_decided_ts_ms", "pnl_lamports", "net_return", "exit_reason")
        .collect()
    )
    if trades.is_empty():
        return None
    gates = (
        pl.scan_parquet(sorted(gates_dir.glob("*/*.parquet")))
        .filter((pl.col("universe") == "labels") & pl.col("passed"))
        .select("mint", "ts_ms", "features")
        .collect()
        .sort("ts_ms")
    )
    joined = trades.sort("entry_decided_ts_ms").join_asof(
        gates, left_on="entry_decided_ts_ms", right_on="ts_ms", by="mint", strategy="backward"
    )
    joined = joined.filter(pl.col("features").is_not_null())
    if joined.is_empty():
        return None
    flat = pl.DataFrame([_flatten(f) for f in joined["features"]], infer_schema_length=None)
    out = pl.concat([joined.drop("features", "ts_ms"), flat], how="horizontal").with_columns(
        run=pl.lit(run.name), win=pl.col("pnl_lamports") > 0
    )
    scores = narrative_scores(run / "events")
    if scores is not None:
        out = out.join(scores.rename({"narrative": "narrative_score"}), on="mint", how="left")
    else:
        out = out.with_columns(narrative_score=pl.lit(None, pl.Int64))
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
    return pl.concat(frames, how="diagonal_relaxed").sort("entry_decided_ts_ms")


def split_table(df: pl.DataFrame) -> list[str]:
    """Each numeric feature split at its median: win rate and expectancy on each side."""
    lines = [
        "| Feature | Below median: n, win rate, expectancy | At or above: n, win rate, "
        "expectancy |",
        "|---|---|---|",
    ]
    skip = {"pnl_lamports", "net_return", "entry_decided_ts_ms", "win"}
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


def report(df: pl.DataFrame) -> list[str]:
    n, wins = df.height, int(df["win"].sum())
    lo, hi = wilson(wins, n)
    counts = df["source"].value_counts().sort("source").rows()
    sources = ", ".join(f"{s}: {c:,}" for s, c in counts)
    return [
        "# Labelled outcomes",
        "",
        "Generated by `uv run python -m cryptobot.labels`. Every token that passed the "
        "conviction gates, bought the moment it qualified with V1's exits (+45% target, sell "
        "at the first price below entry) and the paper fill model. Paper trading only.",
        "",
        f"- Tokens: {n:,} from {df['run'].n_unique()} run(s) ({sources})",
        f"- Win rate: {wins / n:.1%} [{lo:.1%}-{hi:.1%}]",
        f"- Expectancy: {df['net_return'].mean():.1%} per trade, before network costs",
        "",
        "## Each feature split at its median",
        "",
        "A first look, one feature at a time and in sample; overlapping intervals mean no "
        f"clear difference. Features with fewer than {MIN_SPLIT} tokens on a side are left "
        "out.",
        "",
        *split_table(df),
        "",
    ]


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
