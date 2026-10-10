"""Every curve number in CLAUDE.md "Facts to build on", reproduced from the formula."""

import pytest

from cryptobot.curve import (
    CURVE_SALE_TOKENS,
    LAUNCH,
    LAUNCH_MARKET_CAP,
    RAW_PER_TOKEN,
    TOTAL_SUPPLY,
    Curve,
    graduation_curve,
    implied_sol_price,
    launch_multiple,
    to_lamports,
    to_sol,
    usd_to_launch_multiple,
)


def test_launch_reserves():
    assert to_sol(LAUNCH.virtual_sol) == 30
    assert LAUNCH.virtual_tokens == 1_073_000_000 * RAW_PER_TOKEN
    assert CURVE_SALE_TOKENS == 793_100_000 * RAW_PER_TOKEN
    assert TOTAL_SUPPLY == 1_000_000_000 * RAW_PER_TOKEN


def test_launch_market_cap_is_27_96_sol():
    assert to_sol(LAUNCH_MARKET_CAP) == pytest.approx(27.96, abs=0.005)


@pytest.mark.parametrize("multiple", [1, 1.5, 2.5, 4.2, 8.3, 14.7])
def test_market_cap_is_vsol_squared_over_32_19(multiple):
    curve = Curve.at_multiple(multiple)
    vsol = to_sol(curve.virtual_sol)
    assert to_sol(curve.market_cap_lamports()) == pytest.approx(vsol**2 / 32.19, rel=1e-6)


def test_graduation():
    grad = graduation_curve()
    assert to_sol(grad.virtual_sol) == pytest.approx(115, abs=0.01)
    assert to_sol(grad.real_sol) == pytest.approx(85, abs=0.01)
    assert to_sol(grad.market_cap_lamports()) == pytest.approx(410.9, abs=0.05)
    assert launch_multiple(grad) == pytest.approx(14.7, abs=0.05)
    assert grad.progress == pytest.approx(1.0)
    assert grad.graduated


def test_buying_all_curve_tokens_graduates():
    curve, _ = LAUNCH.after_buy(to_lamports(200))
    assert curve.graduated
    assert curve.tokens_sold == CURVE_SALE_TOKENS


def test_round_trip_without_other_trades_returns_the_stake():
    stake = to_lamports(0.4)
    curve, tokens = Curve.at_multiple(8.0).after_buy(stake)
    _, back = curve.after_sell(tokens)
    # Integer flooring may cost a few lamports, never gain any.
    assert stake - 10 <= back <= stake


def test_holder_with_4_4_percent_moves_price_minus_20_at_8_3x():
    curve = Curve.at_multiple(8.3)
    after, _ = curve.after_sell(int(0.044 * TOTAL_SUPPLY))
    assert after.price() / curve.price() - 1 == pytest.approx(-0.20, abs=0.01)


@pytest.mark.parametrize(
    ("usd", "multiple"),
    [(3_500, 1.5), (6_000, 2.5), (7_000, 2.9), (8_000, 3.3), (10_000, 4.2), (20_000, 8.3)],
)
def test_video_levels_at_86_usd_sol(usd, multiple):
    assert usd_to_launch_multiple(usd, 86) == pytest.approx(multiple, abs=0.05)


def test_v1_launch_of_4k_to_5k_implies_sol_at_143_to_179():
    assert implied_sol_price(4_000) == pytest.approx(143, abs=0.5)
    assert implied_sol_price(5_000) == pytest.approx(179, abs=0.5)


@pytest.mark.parametrize(("multiple", "progress"), [(2.0, 0.396), (10.0, 0.925), (14.5, 0.998)])
def test_curve_progress_at_v1_levels(multiple, progress):
    assert Curve.at_multiple(multiple).progress == pytest.approx(progress, abs=0.001)
