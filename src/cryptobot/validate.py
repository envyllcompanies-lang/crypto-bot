"""Data-quality checks for trade-level Pump.fun data (Slinky21/Pumpfun_v2_dataset layout).

Trade rows carry the curve's virtual reserves *after* the trade. The checks rebuild
the reserves before each trade and test them against curve.py and against the
other trades of the same mint.
"""

from __future__ import annotations

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS

SELL = "sell"


def with_before_reserves(trades: pl.DataFrame) -> pl.DataFrame:
    """Add the virtual reserves before each trade, derived from the after-trade reserves."""
    sell = pl.col("ix_name") == SELL
    return trades.with_columns(
        vsol_before=pl.when(sell)
        .then(pl.col("virtual_sol_reserves") + pl.col("sol_amount"))
        .otherwise(pl.col("virtual_sol_reserves") - pl.col("sol_amount")),
        vtok_before=pl.when(sell)
        .then(pl.col("virtual_token_reserves") - pl.col("token_amount"))
        .otherwise(pl.col("virtual_token_reserves") + pl.col("token_amount")),
    )


def _formula_error(ix_name: str, sol: int, tok: int, vs: int, vt: int) -> int:
    """Raw-unit gap between a recorded trade and the constant-product formula.

    Python integers, because reserve products overflow 64 bits. Buys may be priced
    either way round, so a buy matches if the floor of tokens out or the ceiling of
    SOL in agrees. Sells pay the floor of SOL out.
    """
    if ix_name == SELL:
        return abs(tok * vs // (vt + tok) - sol)
    tokens_for_sol = sol * vt // (vs + sol)
    sol_for_tokens = -(-(tok * vs) // (vt - tok)) if vt > tok else sol + 1
    return min(abs(tokens_for_sol - tok), abs(sol_for_tokens - sol))


def curve_math_error(trades: pl.DataFrame) -> pl.DataFrame:
    """Per trade: `err`, the raw-unit gap to the formula, and `k_rel`, the relative
    change in vSol x vTokens across the trade (zero for an exact constant product)."""
    t = with_before_reserves(trades)
    cols = ["ix_name", "sol_amount", "token_amount", "vsol_before", "vtok_before"]
    err = [_formula_error(*row) for row in t.select(cols).iter_rows()]
    k_after = pl.col("virtual_sol_reserves").cast(pl.Float64) * pl.col("virtual_token_reserves")
    k_before = pl.col("vsol_before").cast(pl.Float64) * pl.col("vtok_before")
    return t.with_columns(err=pl.Series(err, dtype=pl.Int64), k_rel=k_after / k_before - 1)


def chain_continuity(trades: pl.DataFrame, creates: pl.DataFrame) -> pl.DataFrame:
    """Per trade: does its before-state equal an after-state already seen for that mint?

    The launch state (30 SOL / 1.073B tokens) counts as seen. This does not need the
    order of trades inside one transaction, which the data does not record.
    """
    t = with_before_reserves(trades)
    after = trades.select(
        "mint",
        vsol_before=pl.col("virtual_sol_reserves"),
        vtok_before=pl.col("virtual_token_reserves"),
    )
    launch = creates.select("mint").with_columns(
        vsol_before=pl.lit(INITIAL_VIRTUAL_SOL, pl.Int64),
        vtok_before=pl.lit(INITIAL_VIRTUAL_TOKENS, pl.Int64),
    )
    known = pl.concat([after, launch]).unique().with_columns(chained=pl.lit(True))
    return t.join(known, on=["mint", "vsol_before", "vtok_before"], how="left").with_columns(
        chained=pl.col("chained").fill_null(False)
    )


def sol_quoted_classic(creates: pl.DataFrame) -> pl.DataFrame:
    """Launches quoted in SOL (30 SOL virtual at create) and not in Mayhem mode."""
    return creates.filter(
        (pl.col("virtual_sol_reserves") == INITIAL_VIRTUAL_SOL) & ~pl.col("is_mayhem_mode")
    )
