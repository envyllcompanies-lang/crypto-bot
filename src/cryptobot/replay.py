"""Event-driven replay of one token's curve trades, with our own orders inserted.

The fill model:
- A decision made at time t lands at t + delay. It executes after every recorded
  trade stamped at or before the landing time, so other traders get in first.
- It fills against the curve as those trades left it, and our own trade moves the
  curve (price impact). Later recorded trades replay on top of the moved curve.
- Stops fill at whatever the curve gives when the sell lands, not at the stop level.
- Fees are charged on the SOL the curve actually takes or pays out. Network costs
  are fixed lamports per transaction. Token-account rent is refunded only if the
  account is closed after the sell.

Recorded trades replay by their inputs: buys by the SOL that reached the curve,
sells by the tokens sold. Amounts are lamports and raw token units throughout.

Positions are also re-checked on a clock: every TICK_MS after the token's last
recorded trade, with no new trade in between, the strategy sees the unchanged curve at
the new time. That lets time-based exits (stall timer, a token gone quiet) fire on
tokens nobody trades any more, as a real bot's timer would. paper.py ticks the same way.

Not modelled yet: trading after graduation (PumpSwap). A position still open when
the token graduates, or when the data ends, is valued by selling into the final
curve state and flagged "graduated" or "open_at_end", so reports can separate it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from cryptobot.costs import TERMINAL_REFERRAL, TOKEN_ACCOUNT_RENT_LAMPORTS, Venue
from cryptobot.curve import Curve


@dataclass(frozen=True)
class Trade:
    """One recorded curve trade by someone else."""

    ts_ms: int
    is_buy: bool
    sol_lamports: int = 0  # buys: lamports that reached the curve, after fees
    token_raw: int = 0  # sells: raw tokens sold
    wallet: str = ""


@dataclass(frozen=True)
class FillConfig:
    delay_ms: int = 2_000
    venue: Venue = TERMINAL_REFERRAL
    network_lamports_per_tx: int = 0
    rent_lamports: int = TOKEN_ACCOUNT_RENT_LAMPORTS
    rent_refunded: bool = True
    max_trades_per_token: int = 1


DEFAULT_FILL = FillConfig()
TICK_MS = 5_000


@dataclass(frozen=True)
class Buy:
    stake_lamports: int
    reason: str = ""


@dataclass(frozen=True)
class Sell:
    reason: str = ""


@dataclass(frozen=True)
class Position:
    entry_ts_ms: int
    tokens: int
    cost_lamports: int  # everything paid: SOL to the curve, fee, network, rent
    entry_price: float  # average fill, lamports per raw token, before fees
    decided_ts_ms: int
    reason: str = ""


@dataclass(frozen=True)
class View:
    """What a strategy sees after each recorded trade."""

    ts_ms: int
    created_ts_ms: int
    curve: Curve
    peak_price: float
    position: Position | None
    order_pending: bool
    last_trade_ms: int | None = None  # the token's last recorded trade; None = this one

    @property
    def quiet_ms(self) -> int:
        """Time since the token's last recorded trade."""
        return 0 if self.last_trade_ms is None else self.ts_ms - self.last_trade_ms

    @property
    def age_ms(self) -> int:
        return self.ts_ms - self.created_ts_ms

    @property
    def price(self) -> float:
        return self.curve.price()


class Strategy(Protocol):
    def decide(self, view: View) -> Buy | Sell | None: ...


@dataclass(frozen=True)
class TradeResult:
    entry_decided_ts_ms: int
    entry_ts_ms: int
    exit_decided_ts_ms: int
    exit_ts_ms: int
    entry_price: float
    exit_price: float
    cost_lamports: int
    proceeds_lamports: int
    entry_reason: str
    exit_reason: str
    flags: tuple[str, ...] = ()

    @property
    def pnl_lamports(self) -> int:
        return self.proceeds_lamports - self.cost_lamports

    @property
    def net_return(self) -> float:
        return self.pnl_lamports / self.cost_lamports


@dataclass
class _Pending:
    order: Buy | Sell
    decided_ts_ms: int
    land_ts_ms: int


@dataclass
class _State:
    curve: Curve
    position: Position | None = None
    pending: _Pending | None = None
    results: list[TradeResult] = field(default_factory=list)
    entries: int = 0


def _fee(lamports: int, venue: Venue) -> int:
    return -(-int(lamports * venue.fee_bps_per_side) // 10_000)


def _fill_buy(state: _State, p: _Pending, cfg: FillConfig) -> None:
    order = p.order
    assert isinstance(order, Buy)
    to_curve = int(order.stake_lamports / (1 + cfg.venue.fee_rate))
    curve, tokens, spent = state.curve.after_buy(to_curve)
    if tokens == 0:
        return
    cost = spent + _fee(spent, cfg.venue) + cfg.network_lamports_per_tx + cfg.rent_lamports
    state.curve = curve
    state.position = Position(
        p.land_ts_ms, tokens, cost, spent / tokens, p.decided_ts_ms, order.reason
    )
    state.entries += 1


def _fill_sell(state: _State, p: _Pending, cfg: FillConfig, flags: tuple[str, ...] = ()) -> None:
    pos = state.position
    assert pos is not None
    curve, gross = state.curve.after_sell(pos.tokens)
    proceeds = gross - _fee(gross, cfg.venue) - cfg.network_lamports_per_tx
    if cfg.rent_refunded:
        proceeds += cfg.rent_lamports
    state.curve = curve
    reason = p.order.reason if isinstance(p.order, Sell) else ""
    state.results.append(
        TradeResult(
            entry_decided_ts_ms=pos.decided_ts_ms,
            entry_ts_ms=pos.entry_ts_ms,
            exit_decided_ts_ms=p.decided_ts_ms,
            exit_ts_ms=p.land_ts_ms,
            entry_price=pos.entry_price,
            exit_price=gross / pos.tokens,
            cost_lamports=pos.cost_lamports,
            proceeds_lamports=proceeds,
            entry_reason=pos.reason,
            exit_reason=reason,
            flags=flags,
        )
    )
    state.position = None


def _land(state: _State, cfg: FillConfig) -> None:
    p = state.pending
    state.pending = None
    if p is None:
        return
    if isinstance(p.order, Buy) and state.position is None:
        _fill_buy(state, p, cfg)
    elif isinstance(p.order, Sell) and state.position is not None:
        _fill_sell(state, p, cfg)


def replay(
    trades: Iterable[Trade],
    strategy: Strategy,
    cfg: FillConfig = DEFAULT_FILL,
    created_ts_ms: int | None = None,
    start: Curve | None = None,
    end_ms: int | None = None,
) -> list[TradeResult]:
    """Replay one token's trades from launch (or `start`) and return our closed trades.

    `end_ms` is when the data ends (default: this token's last trade); clock checks of
    an open position keep running until then.
    """
    events = sorted(trades, key=lambda t: t.ts_ms)
    if not events:
        return []
    created = events[0].ts_ms if created_ts_ms is None else created_ts_ms
    state = _State(curve=start or Curve())
    peak = state.curve.price()
    last_ts = created

    def ticks(until: int) -> None:
        """Clock checks of an open position between the last trade and `until`."""
        t = last_ts + TICK_MS
        while t < until:
            if state.pending and state.pending.land_ts_ms < t:
                _land(state, cfg)
            if state.curve.graduated:
                return
            if state.position is not None and state.pending is None:
                view = View(t, created, state.curve, peak, state.position, False, last_ts)
                action = strategy.decide(view)
                if isinstance(action, Sell):
                    state.pending = _Pending(action, t, t + cfg.delay_ms)
            t += TICK_MS

    for ev in events:
        ticks(ev.ts_ms)
        if state.pending and state.pending.land_ts_ms < ev.ts_ms:
            _land(state, cfg)
        if state.curve.graduated:
            break
        if ev.is_buy:
            state.curve, _, _ = state.curve.after_buy(ev.sol_lamports)
        else:
            state.curve, _ = state.curve.after_sell(ev.token_raw)
        last_ts = ev.ts_ms
        peak = max(peak, state.curve.price())
        if state.curve.graduated:
            break

        view = View(ev.ts_ms, created, state.curve, peak, state.position, state.pending is not None)
        action = None if state.pending else strategy.decide(view)
        if isinstance(action, Buy) and state.position is None:
            if state.entries < cfg.max_trades_per_token:
                state.pending = _Pending(action, ev.ts_ms, ev.ts_ms + cfg.delay_ms)
        elif isinstance(action, Sell) and state.position is not None:
            state.pending = _Pending(action, ev.ts_ms, ev.ts_ms + cfg.delay_ms)

    if end_ms is not None and not state.curve.graduated:
        ticks(end_ms + 1)
    # Orders still in flight land after the last trade.
    if state.pending and not state.curve.graduated:
        _land(state, cfg)
    if state.position is not None:
        flag = "graduated" if state.curve.graduated else "open_at_end"
        p = _Pending(Sell(flag), last_ts, last_ts)
        _fill_sell(state, p, cfg, flags=(flag,))
    return state.results
