"""Print the M1 data-quality report.

Run with `uv run python -m cryptobot.m1_report > reports/<date>-m1-data-quality.md`.
Pass --refresh-chain to re-query the public RPC; otherwise the cached chain check in
data/derived/ is reused. Every number in the report comes from this module.
"""

from __future__ import annotations

import sys

import polars as pl

from cryptobot.chain import check_row, get_transaction
from cryptobot.fetch import RAW, ensure
from cryptobot.validate import chain_continuity, curve_math_error, sol_quoted_classic

TRADES_DS = RAW / "Slinky21__Pumpfun_v2_dataset"
LAUNCHES_DS = RAW / "loopholetape__pumpfun-launches"
DERIVED = RAW.parent / "derived"
# The 12-hour trade sample the report checks; fetched on first run if missing.
TRADE_SAMPLE = (
    "p01_2026-08_to_2026-11/2026-09_d13-24/trade/trade_20260913T031440.parquet",
    "p01_2026-08_to_2026-11/2026-09_d13-24/create/create_20260913T031440.parquet",
)
CHAIN_SAMPLE = 30


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _load_sample() -> tuple[pl.DataFrame, pl.DataFrame]:
    ensure("Slinky21/Pumpfun_v2_dataset", TRADE_SAMPLE)
    ensure("loopholetape/pumpfun-launches")
    trades = pl.read_parquet(sorted(TRADES_DS.rglob("trade_*.parquet")))
    creates = pl.read_parquet(sorted(TRADES_DS.rglob("create_*.parquet")))
    return trades, creates


def trade_sample_section(trades: pl.DataFrame, creates: pl.DataFrame) -> list[str]:
    classic = sol_quoted_classic(creates)
    tc = trades.join(classic.select("mint"), on="mint", how="semi")
    math = curve_math_error(tc)
    cont = chain_continuity(tc, classic)
    by_ix = (
        math.group_by("ix_name")
        .agg(
            n=pl.len(),
            exact=(pl.col("err") == 0).mean(),
            within1=(pl.col("err") <= 1).mean(),
            worst=pl.col("err").max(),
        )
        .sort("n", descending=True)
    )
    dup = (
        trades.height
        - trades.unique(
            ["signature", "mint", "ix_name", "sol_amount", "token_amount", "virtual_sol_reserves"]
        ).height
    )
    sol_quoted = creates.filter(pl.col("virtual_sol_reserves") == 30_000_000_000)
    fees = (
        tc.group_by("fee_basis_points", "creator_fee_basis_points")
        .agg(n=pl.len())
        .sort("n", descending=True)
    )
    other = trades.join(classic.select("mint"), on="mint", how="anti")
    other_fees = (
        other.group_by("fee_basis_points", "creator_fee_basis_points")
        .agg(n=pl.len())
        .sort("n", descending=True)
        .head(5)
    )
    t2022 = (creates["token_program"].str.starts_with("Tokenz")).mean()
    lines = [
        "## Trade-level sample: Slinky21/Pumpfun_v2_dataset",
        "",
        f"One 12-hour file pair: {trades.height:,} trades and {creates.height:,} launches, "
        f"{trades['block_time'].min()} to {trades['block_time'].max()}.",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Launches quoted in SOL (30 SOL virtual at create) | {sol_quoted.height:,} "
        f"({_pct(sol_quoted.height / creates.height)}) |",
        f"| Mayhem launches | {creates['is_mayhem_mode'].sum():,} "
        f"({_pct(creates['is_mayhem_mode'].mean())}) |",
        f"| Classic launches (SOL-quoted, not Mayhem) | {classic.height:,} |",
        f"| Trades on classic launches | {tc.height:,} |",
        f"| Total supply at create, distinct values | "
        f"{sorted(creates['token_total_supply'].unique().to_list())} |",
        f"| Mayhem supply at create | "
        f"{sorted(creates.filter('is_mayhem_mode')['token_total_supply'].unique().to_list())} |",
        f"| Launches on Token-2022 | {_pct(t2022)} |",
        f"| Duplicate trade rows | {dup:,} |",
        f"| Null `tx_index` | {trades['tx_index'].null_count():,} |",
        f"| Classic trades whose before-state continues a known state | "
        f"{_pct(cont['chained'].mean())} |",
        f"| Largest relative change in vSol x vTokens on a classic trade | "
        f"{math['k_rel'].abs().max():.2e} |",
        "",
        "Curve math on classic trades (recorded amount vs constant product, raw units):",
        "",
        "| Instruction | Trades | Exact | Within 1 unit | Worst |",
        "|---|---|---|---|---|",
    ]
    for r in by_ix.iter_rows(named=True):
        lines.append(
            f"| {r['ix_name']} | {r['n']:,} | {_pct(r['exact'])} | {_pct(r['within1'])} "
            f"| {r['worst']} |"
        )
    lines += [
        "",
        "Fees on classic trades (basis points):",
        "",
        "| Protocol | Creator | Trades |",
        "|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {n:,} |" for a, b, n in fees.iter_rows()]
    lines += [
        "",
        "Fees on all other trades (top 5):",
        "",
        "| Protocol | Creator | Trades |",
        "|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {n:,} |" for a, b, n in other_fees.iter_rows()]
    return [*lines, ""]


def chain_section(trades: pl.DataFrame, creates: pl.DataFrame, refresh: bool) -> list[str]:
    cache = DERIVED / "chain_check.parquet"
    if refresh or not cache.exists():
        classic = sol_quoted_classic(creates)
        sample = trades.join(classic.select("mint"), on="mint", how="semi").sample(
            CHAIN_SAMPLE, seed=2026
        )
        results = [
            check_row(row, get_transaction(row["signature"]))
            for row in sample.iter_rows(named=True)
        ]
        DERIVED.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(results).write_parquet(cache)
    res = pl.read_parquet(cache)
    cols = ["found", "slot_match", "time_match", "tx_ok", "event_found", "fields_match"]
    lines = [
        "## Chain check",
        "",
        f"{res.height} random classic trades fetched from the public RPC. Each transaction's",
        "Pump.fun TradeEvent was decoded from its logs and compared with the dataset row",
        "(mint, user, SOL and token amounts, virtual reserves after the trade).",
        "",
        "| Check | Passed |",
        "|---|---|",
    ]
    for c in cols:
        if c in res.columns:
            lines.append(f"| {c} | {int(res[c].fill_null(False).sum())} of {res.height} |")
    return [*lines, ""]


def coverage_section(creates: pl.DataFrame) -> list[str]:
    launches = pl.read_parquet(sorted(LAUNCHES_DS.rglob("launches*/*.parquet")))
    lo = creates["block_time"].min().timestamp()
    hi = creates["block_time"].max().timestamp()
    window = launches.filter(pl.col("t0").is_between(lo, hi))
    a = set(creates["mint"].to_list())
    b = set(window["mint"].to_list())
    both = len(a & b)
    return [
        "## Coverage cross-check",
        "",
        "Launches each dataset recorded in the trade sample's 12-hour window. They come from",
        "two independent recorders, so missing mints on either side show coverage gaps.",
        "",
        "| | Launches |",
        "|---|---|",
        f"| Slinky21 create events | {len(a):,} |",
        f"| loopholetape launches | {len(b):,} |",
        f"| In both | {both:,} |",
        f"| Only in Slinky21 | {len(a - b):,} |",
        f"| Only in loopholetape | {len(b - a):,} |",
        "",
    ]


def launches_section() -> list[str]:
    lines = ["## Launch-level dataset: loopholetape/pumpfun-launches", ""]
    lines += [
        "| Subset | Rows | First launch | Last launch | Classic | Classic graduated |",
        "|---|---|---|---|---|---|",
    ]
    for subset in ("launches_legacy", "launches"):
        df = pl.read_parquet(sorted((LAUNCHES_DS / "data" / subset).glob("*.parquet")))
        classic = df.filter((pl.col("quote_asset") == "sol") & (pl.col("mayhem") == 0))
        grad = classic["graduated"].fill_null(0).mean()
        lines.append(
            f"| {subset} | {df.height:,} | {df['launched_at'].min()} | {df['launched_at'].max()} "
            f"| {classic.height:,} ({_pct(classic.height / df.height)}) | {_pct(grad)} |"
        )
    lines += [
        "",
        "One row per launch: outcomes (graduated, rugged, peak reserve) and first-minute",
        "features. No individual trades, so it gives base rates and labels, not dip replays.",
        "Rows before 2026-09-27 miss about 48% of buys (the card's KI-02).",
        "",
    ]
    return lines


def main(argv: list[str]) -> None:
    trades, creates = _load_sample()
    lines = [
        "# M1: data quality",
        "",
        "Generated by `uv run python -m cryptobot.m1_report`. Do not edit by hand.",
        "",
        *trade_sample_section(trades, creates),
        *chain_section(trades, creates, refresh="--refresh-chain" in argv),
        *coverage_section(creates),
        *launches_section(),
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1:])
