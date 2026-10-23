"""Print the M5 recorder report: what was captured, and whether it can be trusted.

Run with `uv run python -m cryptobot.m5_report > reports/<date>-m5-recorder.md` after
recording with `uv run python -m cryptobot.recorder`. Every number comes from the
files in data/live/ through this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_SOL
from cryptobot.recorder import LIVE
from cryptobot.validate import chain_continuity, curve_math_error, sol_quoted_classic

_AMOUNTS = ("sol_amount", "token_amount", "virtual_sol_reserves", "virtual_token_reserves")


def load(root: Path = LIVE) -> dict[str, pl.DataFrame]:
    tables = ("create", "trade", "sessions")
    return {t: pl.read_parquet(root / t / "**" / "*.parquet") for t in tables}


def _pct(x: float | None) -> str:
    return f"{(x or 0.0) * 100:.2f}%"


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def classic_trades(
    trades: pl.DataFrame, creates: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Classic launches created during recording, and every trade on them.

    Only these can be checked end to end: their whole life since launch is in the capture.
    """
    creates = creates.with_columns(pl.col("virtual_sol_reserves").cast(pl.Int64))
    classic = sol_quoted_classic(creates)
    trades = trades.with_columns(pl.col(c).cast(pl.Int64) for c in _AMOUNTS)
    return classic, trades.filter(pl.col("mint").is_in(classic["mint"]))


def capture_section(creates: pl.DataFrame, trades: pl.DataFrame, s: pl.DataFrame) -> list[str]:
    s = s.sort("connected_ms")
    covered_ms = (s["disconnected_ms"] - s["connected_ms"]).sum()
    span_ms = s["disconnected_ms"].max() - s["connected_ms"].min()
    dups = trades.height - trades.unique(["signature", "event_index"]).height
    lines = [
        "## Capture",
        "",
        "| | |",
        "|---|---|",
        f"| From | {_ts(s['connected_ms'].min())} |",
        f"| To | {_ts(s['disconnected_ms'].max())} |",
        f"| Connections | {s.height} |",
        f"| Time connected | {_pct(covered_ms / span_ms)} of the span |",
        f"| Transactions seen | {s['txs'].sum():,} |",
        f"| Failed transactions (no events) | {s['failed_txs'].sum():,} |",
        f"| Transactions with truncated logs | {s['truncated_txs'].sum():,} |",
        f"| Create events | {creates.height:,} |",
        f"| Trade events | {trades.height:,} |",
        f"| Duplicate trade rows | {dups:,} |",
        "",
        "How each connection ended:",
        "",
        "| Reason | Count |",
        "|---|---|",
    ]
    for reason, n in s.group_by("end_reason").len().sort("len", descending=True).iter_rows():
        lines.append(f"| {reason} | {n} |")
    supply = sorted(creates["token_total_supply"].unique())
    sol_quoted = (creates["virtual_sol_reserves"] == INITIAL_VIRTUAL_SOL).mean()
    lines += [
        "",
        "## Launches",
        "",
        "| | |",
        "|---|---|",
        f"| Total supply at create, distinct values | {supply} |",
        f"| Mayhem launches | {_pct(creates['is_mayhem_mode'].mean())} |",
        f"| Quoted in SOL (30 SOL virtual at create) | {_pct(sol_quoted)} |",
        "",
    ]
    return lines


def checks_section(creates: pl.DataFrame, trades: pl.DataFrame) -> list[str]:
    classic, tc = classic_trades(trades, creates)
    lines = [
        "## Checks on classic launches created during recording",
        "",
        f"{classic.height:,} launches, {tc.height:,} trades. Their whole life since launch is",
        "in the capture, so a missed trade would break the chain of reserves below.",
        "",
    ]
    if tc.is_empty():
        return [*lines, "No trades to check.", ""]
    by_ix = (
        curve_math_error(tc)
        .group_by("ix_name")
        .agg(
            n=pl.len(),
            exact=(pl.col("err") == 0).mean(),
            ok=(pl.col("err") <= 1).mean(),
            worst=pl.col("err").max(),
        )
        .sort("n", descending=True)
    )
    lines += [
        "| Instruction | Trades | Exact | Within 1 raw unit | Worst gap |",
        "|---|---|---|---|---|",
    ]
    for ix, n, exact, ok, worst in by_ix.iter_rows():
        lines.append(f"| {ix} | {n:,} | {_pct(exact)} | {_pct(ok)} | {worst} |")
    chained = chain_continuity(tc, classic.select("mint"))["chained"].mean()
    lines += [
        "",
        f"Trades whose before-state continues a known state: {_pct(chained)}",
        "",
        "Fees (basis points):",
        "",
        "| Protocol | Creator | Trades |",
        "|---|---|---|",
    ]
    fees = tc.group_by("fee_basis_points", "creator_fee_basis_points").len()
    for proto, creator, n in fees.sort("len", descending=True).iter_rows():
        lines.append(f"| {proto} | {creator} | {n:,} |")
    return [*lines, ""]


def report(data: dict[str, pl.DataFrame]) -> list[str]:
    creates, trades = data["create"], data["trade"]
    return [
        "# M5: live recorder",
        "",
        "Generated by `uv run python -m cryptobot.m5_report`. Do not edit by hand.",
        "",
        *capture_section(creates, trades, data["sessions"]),
        *checks_section(creates, trades),
    ]


def main() -> None:
    print("\n".join(report(load())))


if __name__ == "__main__":
    main()
