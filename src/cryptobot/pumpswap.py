"""PumpSwap pools right after graduation, from Slinky21's `graduate` and `swap` tables.

When a curve completes, its reserves move into a new PumpSwap pool (the `graduate`
event: `sol_amount` lamports and `mint_amount` raw tokens). The screen used to value a
position still open at that moment at the curve's final price; this module lets it
sell into the pool instead, at the moment its order would really land.

The swap table holds every Solana DEX swap (about 2.3 GB per 12-hour file), far more
than fits on disk here, so `extract` downloads one file at a time, keeps only swaps in
pools that graduated in the data and only for WINDOW_MS after graduation, writes that
small slice to data/derived/pumpswap/, and deletes the raw file.

Pool reserves are solved from each swap on its own (`pool_path`): the constant product
comes from the deposit, and a swap's amounts then fix the reserves. On 523 pools of the
25 Sep file this predicted the next swap's output to a median 0.02% (2-4% of swaps off
by more than 5%), where adding up swap amounts drifted to a median 14% after 15 min.

Usage: uv run python -m cryptobot.pumpswap [--from 2026-09-01] (resumable)
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from cryptobot.fetch import download, list_files, lock
from cryptobot.slinky import DATASET, SLINKY_ROOT, STAMP

OUT = SLINKY_ROOT.parent.parent / "derived" / "pumpswap"
GRADUATES = OUT / "graduates.parquet"
WINDOW_MS = 70 * 60_000  # keep each pool's first 70 minutes
WSOL = "So11111111111111111111111111111111111111112"
# PumpSwap's fee per swap at low market caps (CLAUDE.md "Costs"; recheck the fee page).
POOL_FEE_BPS = 125
MIN_ANCHOR_LAMPORTS = 10_000_000  # swaps under 0.01 SOL are too rounded to solve for reserves


def _remote(table: str) -> list[dict]:
    return [f for f in list_files(DATASET, "p01_2026-08_to_2026-11") if f"/{table}/" in f["path"]]


def graduates(out: Path = GRADUATES) -> pl.DataFrame:
    """Every graduation event (small table), downloaded once and kept."""
    if out.exists():
        return pl.read_parquet(out)
    frames = []
    for f in _remote("graduate"):
        path = download(DATASET, f["path"], f["size"])
        frames.append(pl.read_parquet(path))
    df = (
        pl.concat(frames, how="diagonal_relaxed")
        .select(
            "mint",
            "pool",
            "sol_amount",
            "mint_amount",
            grad_slot="slot",
            grad_ms=pl.col("ingested_at").dt.epoch("ms"),
        )
        .unique("mint", keep="first")
        .sort("grad_ms")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    return df


def slice_swaps(swap_path: Path, grads: pl.DataFrame) -> pl.DataFrame:
    """Swaps in graduated pools within WINDOW_MS of graduation, as SOL/token flows."""
    sol_in = pl.col("input_mint") == WSOL
    return (
        pl.scan_parquet(swap_path)
        .select(
            "pool",
            "slot",
            "tx_index",
            "input_mint",
            "input_amount",
            "output_amount",
            ts=pl.col("ingested_at").dt.epoch("ms"),
        )
        .join(grads.lazy().select("pool", "grad_slot", "grad_ms"), on="pool", how="inner")
        # By slot, not arrival time: some swaps arrive before the graduation event does.
        .filter(
            (pl.col("slot") >= pl.col("grad_slot"))
            & (pl.col("ts") <= pl.col("grad_ms") + WINDOW_MS)
        )
        .select(
            "pool",
            "slot",
            "tx_index",
            "ts",
            sol_in=pl.when(sol_in).then(pl.col("input_amount")).otherwise(0),
            sol_out=pl.when(sol_in).then(0).otherwise(pl.col("output_amount")),
            tok_in=pl.when(sol_in).then(0).otherwise(pl.col("input_amount")),
            tok_out=pl.when(sol_in).then(pl.col("output_amount")).otherwise(0),
        )
        # Smaller swaps can't anchor the reserves (pool_path), so don't keep them.
        .filter(
            (pl.col("sol_in") >= MIN_ANCHOR_LAMPORTS) | (pl.col("sol_out") >= MIN_ANCHOR_LAMPORTS)
        )
        .collect()
    )


def extract(start: datetime | None = None, out: Path = OUT) -> None:
    """Download, slice and delete each swap file in turn; skip slices already made."""
    grads = graduates()
    out.mkdir(parents=True, exist_ok=True)
    handle = lock(DATASET)  # one fetch of this dataset at a time; released on exit
    try:
        for f in sorted(_remote("swap"), key=lambda f: f["path"]):
            m = STAMP.search(f["path"])
            if m is None:
                continue
            stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            if start is not None and stamp < start:
                continue
            part = out / f"swaps-{m.group(1)}.parquet"
            if part.exists():
                continue
            raw = download(DATASET, f["path"], f["size"])
            df = slice_swaps(raw, grads)
            tmp = part.with_suffix(".tmp")
            df.write_parquet(tmp)
            tmp.rename(part)
            raw.unlink()
            print(
                f"{part.name}: {df.height:,} swaps in {df['pool'].n_unique():,} pools", flush=True
            )
    finally:
        handle.close()


Pool = tuple[int, int, np.ndarray, np.ndarray, np.ndarray]


def load(mints: list[str] | None = None, out: Path = OUT) -> dict[str, Pool]:
    """Per graduated mint (all, or just `mints`): (deposit lamports, deposit tokens, anchor
    times, SOL reserve, token reserve)."""
    grads = pl.read_parquet(out / "graduates.parquet")
    if mints is not None:
        grads = grads.filter(pl.col("mint").is_in(mints))
    swaps = (
        pl.scan_parquet(sorted(out.glob("swaps-*.parquet")))
        .filter(pl.col("pool").is_in(grads["pool"].to_list()))
        .unique(["pool", "slot", "tx_index", "sol_in", "sol_out", "tok_in", "tok_out"])
        .sort(["pool", "slot", "tx_index"])
        .collect()
    )
    by_pool = {k[0]: g for k, g in swaps.group_by(["pool"])}
    paths = {}
    for mint, pool, sol, tok in grads.select(
        "mint", "pool", "sol_amount", "mint_amount"
    ).iter_rows():
        paths[mint] = (sol, tok, *pool_path(sol, tok, by_pool.get(pool)))
    return paths


def pool_path(
    sol: int, tok: int, swaps: pl.DataFrame | None, fee_bps: int = POOL_FEE_BPS
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pool reserves after each swap of at least MIN_ANCHOR_LAMPORTS, from that swap alone.

    Adding up swap amounts drifts (some pool activity is missing from the data), so each
    swap is solved on its own: the pool's constant product k comes from the deposit, and
    a swap's input and output then fix the reserves at that moment. Checked by predicting
    each swap's output from the previous anchor (tests/test_pumpswap.py).
    """
    empty = np.empty(0, np.float64)
    if swaps is None or swaps.is_empty() or sol <= 0 or tok <= 0:
        return np.empty(0, np.int64), empty, empty
    k = float(sol) * float(tok)
    f = fee_bps / 10_000
    si, to, ti, so = (
        swaps[c].to_numpy().astype(np.float64) for c in ("sol_in", "tok_out", "tok_in", "sol_out")
    )
    big = ((si >= MIN_ANCHOR_LAMPORTS) & (to > 0)) | ((so >= MIN_ANCHOR_LAMPORTS) & (ti > 0))
    with np.errstate(all="ignore"):
        a = si * (1 - f)
        s_buy = (-a + np.sqrt(a * a + 4 * a * k / to)) / 2 + a
        d = so / (1 - f)
        s_sell = (d * ti + np.sqrt(d * d * ti * ti + 4 * ti * d * k)) / (2 * ti) - d
    s_after = np.where(si > 0, s_buy, s_sell)
    ok = big & np.isfinite(s_after) & (s_after > 0)
    ts = np.maximum.accumulate(swaps["ts"].to_numpy())[ok]
    s_after = s_after[ok]
    return ts, s_after, k / s_after


def state_at(
    sol: int, tok: int, ts: np.ndarray, s: np.ndarray, t: np.ndarray, at_ms: int
) -> tuple[float, float]:
    """Pool reserves at `at_ms`: the median of the last three anchors (one bad swap can't
    move it), or the deposit if no swap came before."""
    i = int(np.searchsorted(ts, at_ms, side="right"))
    if i == 0:
        return float(sol), float(tok)
    lo = max(0, i - 3)
    j = lo + int(np.argsort(s[lo:i])[(i - lo) // 2])
    return float(s[j]), float(t[j])


def sell_into(sol: float, tok: float, tokens: int, fee_bps: float = POOL_FEE_BPS) -> int:
    """Lamports for selling `tokens` into a constant-product pool, after the swap fee."""
    if sol <= 0 or tok <= 0:
        return 0
    gross = int(tokens * sol / (tok + tokens))
    return gross - int(-(-gross * fee_bps // 10_000))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from",
        dest="start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC),
    )
    args = parser.parse_args(argv)
    extract(args.start)


if __name__ == "__main__":
    main()
