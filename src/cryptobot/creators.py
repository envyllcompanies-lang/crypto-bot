"""Creator records: how often each token creator's earlier launches graduated.

Creator history is one of the best-supported predictors of graduation: tokens from
creators with earlier graduates graduate far more often (Marino et al., arXiv
2602.14860, 655,770 tokens, tested out of sample), and a vendor measured 12% graduation
for creators with an earlier graduate and no launch in the previous 7 days, against a
1.71% base rate. Serial launchers are the opposite case.

The nightly job scans recent recordings: every classic launch per creator, and whether
it graduated (its curve sold out: virtual token reserves reached the graduation level).
Creators sharing a funder are not grouped yet, so one person with many wallets counts as
many creators.

Usage: uv run python -m cryptobot.creators [--days 14]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

from cryptobot.curve import GRADUATION_VIRTUAL_TOKENS
from cryptobot.recorder import LIVE
from cryptobot.tracked import DERIVED, PAPER, _classic, _read, _recent

CREATORS_PATH = DERIVED / "creators.parquet"
RECENT_LAUNCH_MS = 7 * 24 * 3_600_000


def records(creates: pl.DataFrame, trades: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    """Per creator: classic launches, how many graduated, and the newest launch time."""
    launches = _classic(creates).join(
        creates.select("mint", launch_ms=pl.col("received_ms")).unique("mint"), on="mint"
    )
    graduated = (
        trades.lazy()
        .group_by("mint")
        .agg(min_vtok=pl.col("virtual_token_reserves").min())
        .filter(pl.col("min_vtok") <= GRADUATION_VIRTUAL_TOKENS)
        .select("mint", graduated=pl.lit(True))
        .collect(streaming=True)
    )
    return (
        launches.join(graduated, on="mint", how="left")
        .group_by("creator")
        .agg(
            launches=pl.len(),
            graduated=pl.col("graduated").fill_null(False).sum(),
            last_launch_ms=pl.col("launch_ms").max(),
        )
        .sort("creator")
    )


def load_recorded(days: int = 14) -> tuple[pl.DataFrame, pl.LazyFrame]:
    roots = [LIVE, *sorted(PAPER.glob("*/events"))]
    creates = _read([f for r in roots for f in _recent(r, "create", days)])
    trade_files = [f for r in roots for f in _recent(r, "trade", days)]
    if creates is None or not trade_files:
        raise FileNotFoundError("no recorded create and trade events found")
    trades = pl.concat(
        [pl.scan_parquet(f).select("mint", "virtual_token_reserves") for f in trade_files],
        how="diagonal_relaxed",
    )
    return creates.unique("mint"), trades


def save(df: pl.DataFrame, meta: dict, path: Path = CREATORS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.rename(path)
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1))


def load(path: Path = CREATORS_PATH) -> dict[str, tuple[int, int, int]] | None:
    """creator -> (launches, graduated, last launch ms), or None if no file yet.

    Only creators that can change a score are kept: those with a graduate, or a launch
    within RECENT_LAUNCH_MS of the file's newest launch. The rest score as unknown
    creators do, and dropping them keeps the paper trader's memory small.
    """
    if not path.exists():
        return None
    df = pl.read_parquet(path)
    newest = df["last_launch_ms"].max() if df.height else 0
    df = df.filter(
        (pl.col("graduated") > 0) | (pl.col("last_launch_ms") >= newest - RECENT_LAUNCH_MS)
    )
    return {
        c: (int(n), int(g), int(t))
        for c, n, g, t in df.select("creator", "launches", "graduated", "last_launch_ms").rows()
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--out", type=Path, default=CREATORS_PATH)
    args = parser.parse_args(argv)
    try:
        creates, trades = load_recorded(args.days)
    except FileNotFoundError as e:
        print(f"creator records: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    df = records(creates, trades)
    meta = {
        "days": args.days,
        "creators": df.height,
        "launches": int(df["launches"].sum()),
        "graduated": int(df["graduated"].sum()),
        "creators_with_a_graduate": int((df["graduated"] > 0).sum()),
    }
    save(df, meta, args.out)
    print("# Creator records", "", *(f"- {k}: {v:,}" for k, v in meta.items()), sep="\n")


if __name__ == "__main__":
    main()
