import pytest

from cryptobot.curve import TOTAL_SUPPLY, to_lamports
from cryptobot.features import TokenState
from cryptobot.m6_report import wilson
from cryptobot.v1 import (
    FINAL_STRETCH_MIN_MULT,
    FINAL_STRETCH_STAKE,
    MAX_STAKE,
    NEW_PAIRS_MIN_MULT,
    NEW_PAIRS_STAKE,
    NOT_EVALUATED,
    Universe,
    Variant,
    check,
    column_window,
    default_variants,
    passes,
    variant,
)
from tests.test_paper import Tape, create_row


def token_from(tape: Tape, creator="dev") -> TokenState:
    tok = TokenState.from_create(create_row(creator=creator))
    for row in tape.rows:
        tok.apply_trade(row)
    return tok


def test_dollar_levels_use_v1_launch_price():
    assert NEW_PAIRS_MIN_MULT == pytest.approx(7_000 / 4_500)
    assert FINAL_STRETCH_MIN_MULT == pytest.approx(8_000 / 4_500)


def test_variant_grid_respects_sizes_and_cap():
    grid = default_variants()
    assert len(grid) == 4 * (4 * 3 * 2 + 4)
    assert len({v.name for v in grid}) == len(grid)
    assert all(v.strategy.stake_lamports <= MAX_STAKE for v in grid)
    stakes = {v.universe.kind: v.strategy.stake_lamports for v in grid}
    assert stakes == {"new_pairs": NEW_PAIRS_STAKE, "final_stretch": FINAL_STRETCH_STAKE}


def test_stakes_above_the_hard_cap_are_refused():
    with pytest.raises(ValueError):
        default_variants(to_lamports(2.0))
    with pytest.raises(ValueError):
        variant(Universe("x", "new_pairs", to_lamports(1.6)), 0.4, 0.45, 0.0)
    assert isinstance(default_variants(to_lamports(1.5))[0], Variant)


def test_insiders_dev_share_and_pro_traders():
    t = Tape()
    t.buy(0, 1.0, user="dev", slot=100)
    t.buy(10, 0.3, user="sniper", slot=101)
    t.buy(20, 0.3, user="late", slot=110)
    t.buy(30, 0.3, user="late", slot=111)
    tok = token_from(t)
    assert tok.insiders == {"sniper"}
    assert tok.dev_share == pytest.approx(t.held["dev"] / TOTAL_SUPPLY)
    expected = (t.held["dev"] + t.held["sniper"]) / TOTAL_SUPPLY
    assert tok.insider_share == pytest.approx(expected)
    assert tok.pro_traders == 2  # dev bought 1 SOL; late reached 0.6 SOL over two buys
    assert tok.largest_holder()[0] == "dev"


def test_bundle_candle_needs_a_run_with_no_sells():
    run = Tape()
    run.buy(0, 15.0, user="bundle")
    assert token_from(run).bundle_candle

    early_sell = Tape()
    early_sell.buy(0, 1.0, user="a")
    early_sell.sell(10, "a", share=0.5)
    early_sell.buy(20, 15.0, user="b")
    assert not token_from(early_sell).bundle_candle


def test_new_pairs_rules():
    u = Universe("new_pairs", "new_pairs", NEW_PAIRS_STAKE)
    t = Tape()
    t.buy(0, 0.5, user="a")
    tok = token_from(t)
    checks = check(u, tok, now_ms=1_000)
    assert checks["mcap_ge_7k"] is False and not passes(checks)
    assert all(checks[name] is None for name in NOT_EVALUATED)
    t.sell(10, "a", share=0.1)
    t.buy(20, 8.0, user="b")
    tok = token_from(t)
    assert passes(check(u, tok, now_ms=1_000))
    assert not passes(check(u, tok, now_ms=41 * 60 * 1000))  # past the age cap
    tok.tainted = "gap"
    assert not passes(check(u, tok, now_ms=1_000))


def test_final_stretch_column_skips_top_two_keeps_bottom_four():
    members = {m: (float(10 - i), 0) for i, m in enumerate("abcdefg")}
    assert column_window(dict(members), now_ms=1_000) == {"d", "e", "f", "g"}
    three = {m: members[m] for m in "abc"}
    assert column_window(three, now_ms=1_000) == {"c"}
    aged = {"old": (5.0, 0), "new": (4.0, 50 * 60 * 1000)}
    assert column_window(aged, now_ms=60 * 60 * 1000) == set()
    assert "old" not in aged


def test_wilson_interval():
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)
