"""The fast screen agrees with replay.replay on the same token and rule."""

import random

import numpy as np
import pytest

from cryptobot.costs import TERMINAL_REFERRAL
from cryptobot.curve import Curve, to_lamports
from cryptobot.replay import FillConfig, Trade, replay
from cryptobot.screen import Entry, Exit, Params, entry_index, simulate
from cryptobot.strategies import Breakout

STAKE = to_lamports(0.25)


def token(seed: int, n: int = 400, drift: float = 0.55):
    """A random token: recorded trades and the reserves after each one."""
    rng = random.Random(seed)
    curve, t = Curve(), 1_000
    trades, ts, vsol, vtok = [], [], [], []
    held = 0
    for _ in range(n):
        t += rng.randint(50, 3_000)
        if held == 0 or rng.random() < drift:
            sol = rng.randint(10**8, 2 * 10**9)
            curve, out, spent = curve.after_buy(sol)
            held += out
            trades.append(Trade(t, True, sol_lamports=spent))
        else:
            sell = rng.randint(1, held)
            curve, _ = curve.after_sell(sell)
            held -= sell
            trades.append(Trade(t, False, token_raw=sell))
        ts.append(t)
        vsol.append(curve.virtual_sol)
        vtok.append(curve.virtual_tokens)
        if curve.graduated:
            break
    return trades, np.array(ts), np.array(vsol), np.array(vtok)


CASES = [
    (Entry("e", 0.10, 10 * 60_000), Exit("x", target=0.25, stop=0.15)),
    (Entry("e", 0.25, 10 * 60_000), Exit("x", stop=0.30, trail=0.25)),
    (Entry("e", 0.10, 10 * 60_000), Exit("x", trail=0.30, ride=0.95)),
    (Entry("e", 0.50, 30 * 60_000), Exit("x", target=1.0, stop=0.30)),
]


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("case", range(len(CASES)))
def test_screen_matches_replay(seed, case):
    entry, exit_ = CASES[case]
    trades, ts, vsol, vtok = token(seed)
    got = simulate(ts, vsol, vtok, 1_000, (entry,), (exit_,), (TERMINAL_REFERRAL,), Params())
    strat = Breakout(
        STAKE,
        progress=entry.progress,
        max_age_ms=entry.max_age_ms,
        target=exit_.target,
        stop=exit_.stop,
        trail=exit_.trail,
        ride=exit_.ride,
        timeout_ms=exit_.timeout_ms,
    )
    want = replay(trades, strat, FillConfig(delay_ms=2_000), created_ts_ms=1_000)
    assert len(got) == len(want)
    if want:
        # Same decision times and the same fills.
        assert got[0][4] == want[0].entry_decided_ts_ms
        assert got[0][11] == pytest.approx(want[0].net_return, abs=1e-6)


def test_entry_needs_the_level_within_the_age_limit():
    ts = np.array([1_000, 70_000, 200_000])
    prog = np.array([0.05, 0.12, 0.30])
    assert entry_index(Entry("a", 0.10, 60_000), ts, prog, 0) == -1  # reached at 70 s
    assert entry_index(Entry("b", 0.10, 120_000), ts, prog, 0) == 1
    assert entry_index(Entry("c", None, 120_000, min_age_ms=15_000), ts, prog, 0) == 1


def test_buy_that_lands_after_graduation_is_skipped():
    trades, ts, vsol, vtok = token(3, drift=0.95, n=2_000)
    assert Curve(int(vsol[-1]), int(vtok[-1])).graduated
    late = Entry("late", 0.999, 10**9)
    assert simulate(ts, vsol, vtok, 1_000, (late,), (Exit("x", stop=0.3),)) == []
