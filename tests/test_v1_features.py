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


def test_full_grid_is_kept_for_backtests():
    from cryptobot.v1 import full_grid

    grid = full_grid()
    assert len(grid) == 4 * (4 * 3 * 2 + 4) + 3 * 3 * 2 + 3 == 133
    assert len({v.name for v in grid}) == len(grid)


def test_variant_grid_respects_sizes_and_cap():
    grid = default_variants()
    # 2 elite x 2 exits x 2 venues, 1 shadow labels, 2 late-curve universes x 2 exits.
    assert len(grid) == 2 * 2 * 2 + 1 + 2 * 2 == 13
    assert "late_p90_leader|tp25|sl15|d2s|direct" in names_of(grid)
    names = {v.name for v in grid}
    assert "elite_thesis|dip0|tp5|sl10|d2s|direct" in names
    assert "elite_onchain|dip0|trail20|sl10|d2s" in names
    assert len({v.name for v in grid}) == len(grid)
    assert all(v.strategy.stake_lamports <= MAX_STAKE for v in grid)
    stakes = {v.universe.kind: v.strategy.stake_lamports for v in grid}
    assert stakes == {"conviction": NEW_PAIRS_STAKE, "late": NEW_PAIRS_STAKE}
    assert "labels|tp5|sl10|d2s" in names


def test_old_live_grid_is_kept_for_replays():
    from cryptobot.v1 import live_grid_14_oct

    grid = live_grid_14_oct()
    assert len(grid) == 4 + 3 * 2 + 2 * 2 * 2 + 3 + 2 * 2 == 25
    stakes = {v.universe.kind: v.strategy.stake_lamports for v in grid}
    assert stakes["final_stretch"] == FINAL_STRETCH_STAKE
    assert "conviction|tp45|sl0|d2s" in names_of(grid)


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


def test_copycat_window():
    from cryptobot.features import COPYCAT_WINDOW_MS, CopycatWindow

    w = CopycatWindow()
    assert not w.seen("Moon Cat", "MCAT", 0)
    assert w.seen("  moon cat ", "OTHER", 1_000)  # same name, case and spaces aside
    assert w.seen("Different", "mcat", 2_000)  # same symbol
    assert not w.seen("", "", 3_000) and not w.seen(None, None, 3_500)  # blanks never match
    assert not w.seen("Moon Cat", "MCAT", 2_000 + COPYCAT_WINDOW_MS + 1)  # all aged out
    small = CopycatWindow(max_keys=2)
    small.seen("a", "A", 0)
    small.seen("b", "B", 1)
    assert not small.seen("a", "Z", 2)  # memory bound pushed the oldest out


def test_engine_marks_copycats_on_create():
    from cryptobot.paper import Engine
    from cryptobot.v1 import ALL

    eng = Engine([variant(ALL, 0.4, 0.45, 0.0)])
    eng.on_create(create_row(mint="m1", name="Pepe", symbol="PEPE"))
    eng.on_create(create_row(mint="m2", ts=5, name="Frog", symbol="pepe"))
    assert not eng.tokens["m1"].copycat and eng.tokens["m2"].copycat
    assert eng.stats["copycats"] == 1


def test_every_default_variant_sells_a_token_gone_quiet():
    from cryptobot.v1 import QUIET_MS

    assert all(v.strategy.quiet_ms == QUIET_MS for v in default_variants())
    assert all(v.describe()["quiet_ms"] == QUIET_MS for v in default_variants())


def names_of(grid):
    return {v.name for v in grid}
