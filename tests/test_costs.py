"""Cost facts from CLAUDE.md and the break-even reference, recomputed."""

import pytest

from cryptobot.costs import (
    DIRECT,
    TERMINAL_DEFAULT,
    TERMINAL_REFERRAL,
    break_even_win_rate,
    net_return,
    round_trip_cost,
    terminal,
)
from cryptobot.curve import to_lamports

STAKE = to_lamports(1)


def test_fee_rates():
    assert DIRECT.fee_bps_per_side == 125
    assert TERMINAL_DEFAULT.fee_bps_per_side == pytest.approx(215)
    assert TERMINAL_REFERRAL.fee_bps_per_side == pytest.approx(190)


@pytest.mark.parametrize(
    ("venue", "stated"),
    [(DIRECT, 0.025), (TERMINAL_REFERRAL, 0.038), (TERMINAL_DEFAULT, 0.043)],
)
def test_round_trip_matches_stated_figures(venue, stated):
    # CLAUDE.md adds the two sides; compounding them comes out slightly lower.
    assert round_trip_cost(venue) == pytest.approx(stated, abs=0.001)
    assert round_trip_cost(venue) <= stated


def test_break_even_plus_50_minus_20_through_terminal_is_about_35_percent():
    rate = break_even_win_rate(0.5, -0.2, TERMINAL_DEFAULT, STAKE)
    assert rate == pytest.approx(0.35, abs=0.01)


def test_break_even_when_losses_average_minus_40_is_about_50_percent():
    rate = break_even_win_rate(0.5, -0.4, TERMINAL_DEFAULT, STAKE)
    assert rate == pytest.approx(0.50, abs=0.01)


def test_network_costs_hurt_small_trades_most():
    tx = to_lamports(0.002)
    small = break_even_win_rate(0.45, -0.1, TERMINAL_REFERRAL, to_lamports(0.05), tx)
    large = break_even_win_rate(0.45, -0.1, TERMINAL_REFERRAL, to_lamports(0.4), tx)
    assert small > large


def test_flat_trade_loses_exactly_the_round_trip():
    assert net_return(0.0, DIRECT, STAKE) == pytest.approx(-round_trip_cost(DIRECT))


def test_cashback_must_be_a_share():
    with pytest.raises(ValueError):
        terminal(35)
