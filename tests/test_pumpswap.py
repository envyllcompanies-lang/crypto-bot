"""Pool reserves solved from each swap, and selling into the pool."""

import numpy as np
import polars as pl
import pytest

from cryptobot.pumpswap import pool_path, sell_into, state_at
from cryptobot.screen import NO_FEES, Entry, Exit, Params, simulate
from tests.test_screen import token

SOL, TOK = 85_000_000_000, 206_900_000_000_000
F = 0.0125


def swaps(trades):
    """Constant-product swaps (fee off the input for buys, off the output for sells)."""
    s, t, rows = float(SOL), float(TOK), []
    for k, (kind, amount) in enumerate(trades):
        if kind == "buy":
            a = amount * (1 - F)
            out = a * t / (s + a)
            s, t = s + a, t - out
            rows.append((k, amount, 0, 0, int(out)))
        else:
            gross = amount * s / (t + amount)
            s, t = s - gross, t + amount
            rows.append((k, 0, int(gross * (1 - F)), amount, 0))
    cols = ["ts", "sol_in", "sol_out", "tok_in", "tok_out"]
    return pl.DataFrame(rows, schema=cols, orient="row"), s, t


def test_pool_path_recovers_reserves_from_each_swap():
    df, s, t = swaps([("buy", 2 * 10**9), ("buy", 5 * 10**8), ("sell", 10**13), ("buy", 10**9)])
    ts, ps, pt = pool_path(SOL, TOK, df)
    assert ps[-1] == pytest.approx(s, rel=1e-6)
    assert pt[-1] == pytest.approx(t, rel=1e-6)


def test_dust_swaps_are_not_anchors_and_state_falls_back_to_the_deposit():
    df, _, _ = swaps([("buy", 1_000), ("buy", 2 * 10**9)])
    ts, ps, _ = pool_path(SOL, TOK, df)
    assert ts.tolist() == [1]
    assert state_at(SOL, TOK, ts, ps, ps, 0) == (SOL, TOK)


def test_state_is_the_median_of_the_last_three_anchors():
    ts = np.array([1, 2, 3])
    s = np.array([100.0, 900.0, 110.0])
    assert state_at(SOL, TOK, ts, s, s, 5)[0] == 110.0


def test_sell_into_is_constant_product_less_fee():
    assert sell_into(100.0, 1000.0, 1000, fee_bps=0) == 50
    assert sell_into(100.0, 1000.0, 1000, fee_bps=125) == 50 - 1


def test_a_position_open_at_graduation_sells_into_the_pool():
    _, ts, vsol, vtok = token(3, drift=0.95, n=2_000)
    hold = Exit("hold", timeout_ms=10**9)
    entry = (Entry("e", 0.5, 10**9),)
    plain = simulate(ts, vsol, vtok, 1_000, entry, (hold,), (NO_FEES,), Params())
    # an empty pool path: the sell lands on the deposit itself
    pool = (SOL, TOK, np.empty(0, np.int64), np.empty(0), np.empty(0))
    sold = simulate(ts, vsol, vtok, 1_000, entry, (hold,), (NO_FEES,), Params(), pool=pool)
    assert plain[0][8] == "graduated" and sold[0][8] == "graduated_pool"
    # the pool is shallower than the curve, so the same tokens fetch less
    assert sold[0][10] < plain[0][10]
