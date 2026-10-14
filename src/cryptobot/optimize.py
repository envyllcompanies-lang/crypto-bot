"""Search for the highest-return rule in the history, honestly.

Each rule is one cell of a grid built from the timing map (screen.py --timing):
entry level (curve progress) x holding time x token age at entry x whether a top-50
leader (follow.py) bought first. Fees: direct to the program.

Picking the best of hundreds of cells always flatters it, so:

1. The winner is picked on discovery data only (launches before 24 Sep), among cells
   with at least MIN_TRADES trades there.
2. It is judged on the holdout (24 Sep on), which played no part in picking it.
3. For scale, the report also shows the hindsight winner: the best cell picked on the
   holdout itself. The gap between that and the honest result is what luck adds.

Usage: uv run python -m cryptobot.optimize [--timing data/derived/timing_d0]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from cryptobot.follow import DERIVED, LEADERS, TOP
from cryptobot.screen import DIRECT, DISCOVERY_END, _pct, _summary

MIN_TRADES = 300
AGE_BUCKETS = ((0, 5), (5, 15), (15, 60))  # minutes since launch at the buy decision


def leader_times(follow_dir: Path, top: int = TOP) -> pl.LazyFrame:
    """Per mint, the first buy by a top leader: discovery from the scoring pass (in
    sample for the leader list), holdout from the follow run (out of sample)."""
    board = pl.read_parquet(LEADERS)
    names = board.filter("tracked")["user"].head(top).to_list()
    disc = (
        pl.scan_parquet(sorted((DERIVED / "follow_scores").glob("part-*.parquet")))
        .filter(pl.col("entry").is_in(names))
        .select("mint", "decided_ms")
    )
    hold = (
        pl.scan_parquet(sorted(follow_dir.glob("part-*.parquet")))
        .filter((pl.col("exit") == "leader") & (pl.col("entry") == f"copy_first_top{top}"))
        .select("mint", "decided_ms")
    )
    return pl.concat([disc, hold]).group_by("mint").agg(lead_ms=pl.col("decided_ms").min())


def cells(timing_dir: Path, follow_dir: Path) -> pl.DataFrame:
    """Every timing-map trade with its rule cell and period."""
    cut = int(DISCOVERY_END.timestamp() * 1000)
    age_min = (pl.col("decided_ms") - pl.col("created_ms")) / 60_000
    age = pl.lit(None, dtype=pl.String)
    for lo, hi in reversed(AGE_BUCKETS):
        age = pl.when((age_min >= lo) & (age_min < hi)).then(pl.lit(f"{lo}-{hi}m")).otherwise(age)
    return (
        pl.scan_parquet(sorted(timing_dir.glob("part-*.parquet")))
        .filter(pl.col("venue") == DIRECT.name)
        .select("mint", "entry", "exit", "created_ms", "decided_ms", "net_return", "flag")
        .unique(["mint", "entry", "exit"])
        .join(leader_times(follow_dir), on="mint", how="left")
        .with_columns(
            age=age,
            led=(pl.col("lead_ms") < pl.col("decided_ms")).fill_null(False),
            period=pl.when(pl.col("created_ms") < cut)
            .then(pl.lit("discovery"))
            .otherwise(pl.lit("holdout")),
        )
        .drop_nulls("age")
        .collect()
    )


def search(df: pl.DataFrame, min_trades: int = MIN_TRADES) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Discovery and holdout stats for every cell (led: yes / no / either)."""
    either = df.with_columns(led=pl.lit("either"))
    both = pl.concat(
        [df.with_columns(led=pl.when("led").then(pl.lit("yes")).otherwise(pl.lit("no"))), either]
    )
    keys = ["entry", "exit", "age", "led"]
    s = _summary(both, [*keys, "period"])
    disc = s.filter(pl.col("period") == "discovery").drop("period")
    hold = s.filter(pl.col("period") == "holdout").drop("period")
    joined = disc.join(hold, on=keys, how="full", suffix="_h", coalesce=True)
    honest = joined.filter(pl.col("n") >= min_trades).sort("mean", descending=True)
    hindsight = joined.filter(pl.col("n_h") >= min_trades).sort("mean_h", descending=True)
    return honest, hindsight


def _ci(m: float | None, h: float | None) -> str:
    if m is None or h is None:
        return "n/a"
    return f"{_pct(m)} [{_pct(m - h)} to {_pct(m + h)}]"


def graduation_check(df: pl.DataFrame, cell: dict) -> dict:
    """How much of a cell's result rests on positions open when the curve completed.

    Those are valued at the final curve price; after graduation the token trades on
    PumpSwap, which our data doesn't cover. `needed` is the mean return the graduated
    positions must really achieve for the cell to break even on the holdout.
    """
    keys = ["entry", "exit", "age"]
    c = df.filter(*[pl.col(k) == cell[k] for k in keys], pl.col("period") == "holdout")
    if cell["led"] != "either":
        c = c.filter(pl.col("led") == (cell["led"] == "yes"))
    grad = pl.col("flag") == "graduated"
    g, rest = c.filter(grad), c.filter(~grad)
    share = g.height / c.height if c.height else 0.0
    rest_mean = rest["net_return"].mean() if rest.height else 0.0
    needed = None if share == 0 else -(1 - share) * rest_mean / share
    return {
        "share": share,
        "graduated_mean": g["net_return"].mean() if g.height else None,
        "rest_mean": rest_mean,
        "needed": needed,
    }


def report(
    honest: pl.DataFrame, hindsight: pl.DataFrame, delay_ms: int, grad: dict | None = None
) -> str:
    tried = honest.height
    lines = [
        "# Highest-return search over the history",
        "",
        "Generated by `uv run python -m cryptobot.optimize`. Cells: entry level x holding "
        f"time x age at entry {[f'{a}-{b}m' for a, b in AGE_BUCKETS]} x top-{TOP} leader "
        f"bought first (yes / no / either). Direct fees, {delay_ms / 1000:g} s delay, 0.25 "
        f"SOL. {tried} cells have at least {MIN_TRADES} discovery trades.",
        "",
        "## The honest answer: picked on 1-23 Sep, judged on 24 Sep-8 Oct",
        "",
        "| Rank | Entry | Hold | Age | Leader | Disc. n | Disc. mean [95% CI] | Hold. n "
        "| Hold. mean [95% CI] |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for k, r in enumerate(honest.head(10).iter_rows(named=True), 1):
        lines.append(
            f"| {k} | {r['entry']} | {r['exit']} | {r['age']} | {r['led']} | {r['n']:,} | "
            f"{_ci(r['mean'], r['half'])} | {r['n_h'] or 0:,} | {_ci(r['mean_h'], r['half_h'])} |"
        )
    if honest.height:
        best = honest.row(0, named=True)
        ok = best["mean_h"] is not None and best["mean_h"] - best["half_h"] > 0
        lines += [
            "",
            f"Discovery winner on the holdout: **{_pct(best['mean_h'])}** per trade "
            f"({'positive with its 95% interval above zero' if ok else 'not proven positive'}"
            f", n {best['n_h'] or 0:,}).",
        ]
    if grad is not None and grad["needed"] is not None:
        lines += [
            "",
            f"**Graduation check for the winner (holdout):** {_pct(grad['share'])} of its "
            "trades were still open when the curve completed. They are valued at the final "
            f"curve price ({_pct(grad['graduated_mean'])} on average); the other trades "
            f"averaged {_pct(grad['rest_mean'])}. To break even, the graduated positions "
            f"must really return at least {_pct(grad['needed'])} when sold on PumpSwap, so "
            "this result stands or falls on post-graduation fills, which this data can't "
            "measure.",
        ]
    pos_disc = honest.filter(pl.col("mean") > 0)
    both_pos = pos_disc.filter(pl.col("mean_h") > 0)
    lines += [
        f"- Cells positive on discovery: {pos_disc.height} of {tried}; of those, positive on "
        f"the holdout too: {both_pos.height}.",
        "",
        "## Hindsight, for scale: the best cells picked on the holdout itself",
        "",
        "Not a strategy: these were chosen after seeing the results, which is what a "
        "backtest that tries many rules and reports the best one does.",
        "",
        "| Entry | Hold | Age | Leader | Hold. n | Hold. mean [95% CI] | Same cell on discovery |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in hindsight.head(5).iter_rows(named=True):
        lines.append(
            f"| {r['entry']} | {r['exit']} | {r['age']} | {r['led']} | {r['n_h']:,} | "
            f"{_ci(r['mean_h'], r['half_h'])} | {_ci(r['mean'], r['half'])} |"
        )
    return "\n".join([*lines, ""])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timing", type=Path, default=DERIVED / "timing_d0")
    parser.add_argument("--follow", type=Path, default=DERIVED / "follow_d0")
    parser.add_argument("--delay-ms", type=int, default=0)
    args = parser.parse_args(argv)
    df = cells(args.timing, args.follow)
    honest, hindsight = search(df)
    grad = graduation_check(df, honest.row(0, named=True)) if honest.height else None
    print(report(honest, hindsight, args.delay_ms, grad))


if __name__ == "__main__":
    main()
