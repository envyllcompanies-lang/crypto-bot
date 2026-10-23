"""The replay engine on hand-built toy tokens."""

from dataclasses import dataclass, field

import pytest

from cryptobot.costs import TERMINAL_REFERRAL, round_trip_cost
from cryptobot.curve import TOTAL_SUPPLY, Curve, to_lamports
from cryptobot.replay import Buy, FillConfig, Sell, Trade, View, replay
from cryptobot.strategies import DipBuyer

NO_RENT = FillConfig(delay_ms=1_000, rent_lamports=0)


def tick(ts_ms: int) -> Trade:
    """A recorded trade that moves nothing: a clock tick for the engine."""
    return Trade(ts_ms, is_buy=True, sol_lamports=0)


def buy(ts_ms: int, sol: float) -> Trade:
    return Trade(ts_ms, is_buy=True, sol_lamports=to_lamports(sol))


def sell(ts_ms: int, share_of_supply: float) -> Trade:
    return Trade(ts_ms, is_buy=False, token_raw=int(share_of_supply * TOTAL_SUPPLY))


@dataclass
class Script:
    """Returns a fixed action at given event timestamps."""

    actions: dict[int, Buy | Sell]
    seen: list[View] = field(default_factory=list)

    def decide(self, view: View) -> Buy | Sell | None:
        self.seen.append(view)
        return self.actions.get(view.ts_ms)


def test_round_trip_with_no_other_trades_costs_the_fee_round_trip():
    stake = to_lamports(0.4)
    script = Script({0: Buy(stake), 5_000: Sell()})
    [r] = replay([tick(0), tick(5_000), tick(10_000)], script, NO_RENT, start=Curve.at_multiple(8))
    assert r.net_return == pytest.approx(-round_trip_cost(TERMINAL_REFERRAL), abs=1e-6)


def test_longer_delay_lets_a_competing_buy_land_first():
    events = [tick(0), buy(1_500, 5.0), tick(3_000)]
    stake = to_lamports(0.4)
    prices = {}
    for delay in (1_000, 2_000):
        cfg = FillConfig(delay_ms=delay, rent_lamports=0)
        [r] = replay(events, Script({0: Buy(stake)}), cfg, start=Curve.at_multiple(8))
        prices[delay] = r.entry_price
    assert prices[2_000] > prices[1_000] * 1.05


def test_stop_fills_at_the_first_available_price_not_at_the_stop():
    stake = to_lamports(0.4)
    strategy = DipBuyer(stake, drop=0.0, target=1.0, max_age_ms=None)
    events = [
        tick(0),  # decide to buy at the high (drop 0) -> lands at 2s
        tick(3_000),
        sell(5_000, 0.002),  # small sell: price dips below entry -> stop decided
        sell(5_500, 0.044),  # a big holder dumps before our sell lands at 7s
        tick(9_000),
    ]
    cfg = FillConfig(delay_ms=2_000, rent_lamports=0)
    [r] = replay(events, strategy, cfg, start=Curve.at_multiple(8.3))
    assert r.exit_reason == "stop"
    assert r.exit_decided_ts_ms == 5_000 and r.exit_ts_ms == 7_000
    # The stop was "at entry", but the dump in between filled it far lower.
    assert r.exit_price < r.entry_price * 0.82
    assert r.net_return < -0.2


def test_target_hit_sells_on_the_bounce():
    stake = to_lamports(0.25)
    strategy = DipBuyer(stake, drop=0.0, target=0.45, max_age_ms=None)
    events = [tick(0), tick(2_500)] + [buy(3_000 + i * 1_000, 3.0) for i in range(8)]
    events.append(tick(20_000))
    [r] = replay(events, strategy, FillConfig(rent_lamports=0), start=Curve.at_multiple(4))
    assert r.exit_reason == "target"
    assert r.net_return > 0.3


def test_our_price_impact_at_1_5_sol():
    start = Curve.at_multiple(8)
    stake = to_lamports(1.5)
    [r] = replay([tick(0), tick(5_000)], Script({0: Buy(stake)}), NO_RENT, start=start)
    to_curve = int(stake / (1 + TERMINAL_REFERRAL.fee_rate))
    _, tokens, spent = start.after_buy(to_curve)
    assert r.entry_price == pytest.approx(spent / tokens)
    # Constant product: the average fill sits spent / virtual_sol above the spot price.
    assert r.entry_price / start.price() - 1 == pytest.approx(spent / start.virtual_sol)
    assert r.entry_price / start.price() - 1 > 0.015


def test_buy_near_graduation_is_charged_only_what_completes_the_curve():
    start = Curve.at_multiple(14.65)
    stake = to_lamports(1.5)
    [r] = replay([tick(0), tick(5_000)], Script({0: Buy(stake)}), NO_RENT, start=start)
    assert r.cost_lamports < stake / 2
    assert "graduated" in r.flags


def test_position_open_when_token_graduates_is_flagged():
    stake = to_lamports(0.4)
    events = [tick(0), tick(3_000), buy(4_000, 10.0), tick(6_000)]
    [r] = replay(events, Script({0: Buy(stake)}), NO_RENT, start=Curve.at_multiple(14))
    assert r.flags == ("graduated",)


def test_position_open_at_end_of_data_is_flagged():
    [r] = replay([tick(0), tick(3_000)], Script({0: Buy(to_lamports(0.1))}), NO_RENT)
    assert r.flags == ("open_at_end",)


def test_one_trade_per_token_by_default():
    stake = to_lamports(0.1)
    actions = {t: Buy(stake) for t in range(0, 30_000, 1_000)}
    actions.update({5_000: Sell(), 15_000: Sell()})
    events = [tick(t) for t in range(0, 30_000, 1_000)]
    results = replay(events, Script(actions), NO_RENT)
    assert len(results) == 1


def test_lost_rent_costs_more_than_refunded_rent():
    stake = to_lamports(0.05)
    script = {0: Buy(stake), 5_000: Sell()}
    events = [tick(0), tick(5_000), tick(10_000)]
    kept = replay(events, Script(dict(script)), FillConfig(rent_refunded=True))[0]
    lost = replay(events, Script(dict(script)), FillConfig(rent_refunded=False))[0]
    assert kept.pnl_lamports - lost.pnl_lamports == FillConfig().rent_lamports


RUN_UP = [buy(i * 1_000, 2.0) for i in range(20)]  # launch to 70 virtual SOL


def _drop_from_peak(events: list[Trade]) -> float:
    curve, peak = Curve(), 0.0
    for ev in events:
        if ev.is_buy:
            curve, _, _ = curve.after_buy(ev.sol_lamports)
        else:
            curve, _ = curve.after_sell(ev.token_raw)
        peak = max(peak, curve.price())
    return 1 - curve.price() / peak


def test_dip_buyer_waits_for_the_drop():
    stake = to_lamports(0.25)
    strategy = DipBuyer(stake, drop=0.40, target=0.45, max_age_ms=None)
    shallow = [sell(25_000, 0.06), tick(26_000)]
    deep = [sell(30_000, 0.08), tick(40_000)]
    assert 0.20 < _drop_from_peak(RUN_UP + shallow) < 0.25
    assert _drop_from_peak(RUN_UP + shallow + deep) > 0.40
    results = replay(RUN_UP + shallow + deep, strategy, NO_RENT)
    assert len(results) == 1
    assert results[0].entry_decided_ts_ms == 30_000


def test_dip_buyer_respects_the_age_cap():
    stake = to_lamports(0.25)
    dump = [sell(120_000, 0.14), tick(130_000)]  # a 40%+ dip, two minutes after launch
    assert _drop_from_peak(RUN_UP + dump) > 0.40
    capped = DipBuyer(stake, drop=0.40, max_age_ms=60_000)
    uncapped = DipBuyer(stake, drop=0.40, max_age_ms=None)
    assert replay(RUN_UP + dump, capped, NO_RENT) == []
    assert len(replay(RUN_UP + dump, uncapped, NO_RENT)) == 1
