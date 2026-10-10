"""Trading costs and break-even win rates.

Fees are charged on the SOL side of each trade. On a buy you pay `budget`, the
fee comes out of it, and the rest reaches the curve. On a sell the curve pays
out and the fee comes off what it pays. Network costs (priority fee and tip)
are a fixed number of lamports per transaction, so they weigh most on small
trades.

Rates as of 8 Oct 2026; recheck https://pump.fun/docs/fees before relying on them.
Price impact is not modelled here. The fill model (M2) adds it from the curve.
"""

from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000

PUMP_CURVE_FEE_BPS = 125  # 0.95% protocol + 0.30% creator
TERMINAL_FEE_BPS = 100  # Terminal's own fee, before cashback

# Rent for the token account a first buy creates: a 165-byte SPL token account.
# Token-2022 accounts with extensions cost slightly more; measure on-chain in M5.
# Closing the account after the sell refunds it; leaving it open loses it.
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280


@dataclass(frozen=True)
class Venue:
    name: str
    fee_bps_per_side: float

    @property
    def fee_rate(self) -> float:
        return self.fee_bps_per_side / BPS


def terminal(cashback: float) -> Venue:
    """Trading through Terminal: the curve fee plus Terminal's fee less cashback."""
    if not 0 <= cashback <= 1:
        raise ValueError("cashback is a share between 0 and 1")
    bps = PUMP_CURVE_FEE_BPS + TERMINAL_FEE_BPS * (1 - cashback)
    return Venue(f"Terminal, {cashback:.0%} cashback", bps)


DIRECT = Venue("Direct to the program", PUMP_CURVE_FEE_BPS)
TERMINAL_DEFAULT = terminal(0.10)
TERMINAL_REFERRAL = terminal(0.35)
VENUES = (DIRECT, TERMINAL_REFERRAL, TERMINAL_DEFAULT)


def round_trip_cost(venue: Venue) -> float:
    """Share of the stake lost to fees on a buy and immediate sell, ignoring network costs."""
    f = venue.fee_rate
    return 1 - (1 - f) / (1 + f)


def net_return(
    price_move: float,
    venue: Venue,
    stake_lamports: int,
    network_lamports_per_tx: int = 0,
    rent_lamports: int = 0,
    rent_refunded: bool = True,
) -> float:
    """Return on the stake after fees and two transactions, for a given price move.

    `price_move` is the change between the entry and exit fill prices, e.g. 0.5
    for +50% or -0.2 for -20%. `rent_lamports` is paid on the buy and comes back
    only if the token account is closed after the sell (`rent_refunded`).
    """
    if stake_lamports <= 0:
        raise ValueError("stake must be positive")
    f = venue.fee_rate
    proceeds = stake_lamports / (1 + f) * (1 + price_move) * (1 - f)
    proceeds -= 2 * network_lamports_per_tx
    if not rent_refunded:
        proceeds -= rent_lamports
    return proceeds / stake_lamports - 1


def break_even_win_rate(
    win_move: float,
    loss_move: float,
    venue: Venue,
    stake_lamports: int,
    network_lamports_per_tx: int = 0,
    rent_lamports: int = 0,
    rent_refunded: bool = True,
) -> float:
    """Win rate at which expected profit is zero, for fixed win and loss price moves."""
    extra = (network_lamports_per_tx, rent_lamports, rent_refunded)
    win = net_return(win_move, venue, stake_lamports, *extra)
    loss = net_return(loss_move, venue, stake_lamports, *extra)
    if win <= 0:
        return 1.0
    if loss >= 0:
        return 0.0
    return -loss / (win - loss)
