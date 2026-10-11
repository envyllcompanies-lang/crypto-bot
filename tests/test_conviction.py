import json

import pytest

from cryptobot.conviction import MAX_AGE_MS, blocks, conviction
from cryptobot.features import TokenState
from tests.test_holders import BUNDLE, CLEAN
from tests.test_paper import Tape, create_row

NOW = 6_000


def clean_token():
    """A sell before 2x (no bundle candle), dev holds nothing, no insiders."""
    t = Tape()
    t.buy(1_000, 1.0, user="a")
    t.sell(2_000, "a", share=0.1)
    t.buy(3_000, 8.0, user="b")
    tok = TokenState.from_create(create_row())
    for row in t.rows:
        tok.apply_trade(row)
    return tok


def with_narrative(score, flags=()):
    return {**CLEAN, "narrative": {"score": score, "theme": "t", "red_flags": list(flags)}}


@pytest.mark.parametrize(
    ("result", "score", "tier", "drop"),
    [
        (with_narrative(5), 40 + 12 + 8 + 8 + 8 + 8 + 4 + 8 * 0.2, "very_high", 0.0),
        (with_narrative(4), 30 + 49.6, "very_high", 0.0),  # 79.6 rounds to 80
        (with_narrative(3), 20 + 49.6, "good", 0.25),
        (with_narrative(2), 10 + 49.6, "average", 0.40),
        (with_narrative(1), 49.6, "average", 0.40),
        (with_narrative(1, ["no_story"]), 39.6, "skip", None),
        (with_narrative(5, ["low_effort", "no_story"]), 89.6 - 20, "good", 0.25),
        (CLEAN, 15 + 49.6, "good", 0.25),  # narrative not evaluated: neutral 15
        ({**CLEAN, "x_kind": None}, 15 + 49.6 - 12 + 4, "average", 0.40),
    ],
)
def test_tiers(result, score, tier, drop):
    tok = clean_token()
    assert tok.pro_traders == 2  # "a" and "b" bought 0.5 SOL or more
    c = conviction(tok, result, 4_000, NOW)
    assert c is not None
    assert (c.score, c.tier, c.drop) == (round(score), tier, drop)
    assert sum(c.reasons.values()) == pytest.approx(score, abs=0.01)


def test_copycat_loses_points_and_counts_as_a_red_flag():
    tok = clean_token()
    tok.copycat = True
    c = conviction(tok, with_narrative(5, ["copycat"]), 4_000, NOW)
    assert c.red_flags == ("copycat",)  # not counted twice
    assert c.reasons["not_copycat"] == 0 and c.reasons["red_flags"] == -10


def test_dev_and_insider_points_scale_down():
    tok = clean_token()
    tok.balances["dev"] = int(0.10 * 10**15)  # 10% of supply (6 decimals)
    c = conviction(tok, CLEAN, 4_000, NOW)
    assert c.reasons["dev_share"] == pytest.approx(4.0)  # halfway from 5% to 15%
    assert c.reasons["insider_share"] == 8.0  # 10% is under the 20% line


def test_hard_blocks():
    tok = clean_token()
    assert conviction(tok, None, None, NOW) is None
    assert blocks(tok, None, None, NOW) == ["no_lookup"]
    assert blocks(tok, BUNDLE, 4_000, NOW) == ["funding_clustered"]
    assert blocks(tok, CLEAN, 4_000, MAX_AGE_MS + 1) == ["older_than_40m"]
    tok.tainted = "gap"
    assert conviction(tok, with_narrative(5), 4_000, NOW) is None


# Engine -------------------------------------------------------------------------------

from cryptobot.paper import EVENT_SCHEMAS, OUT_SCHEMAS, Engine, read_table, run_replay  # noqa: E402
from cryptobot.recorder import Sink  # noqa: E402
from cryptobot.v1 import conviction_universe, conviction_variant  # noqa: E402
from tests.test_holders import lookup_row  # noqa: E402

LOOKUP_MS = 4_500


def dump_tape():
    """Passes the entry gate at 3 s; at 6 s the 20 SOL buyer sells half, a ~43% drop
    that stays above the $7K gate."""
    t = Tape()
    t.buy(1_000, 1.0, user="a")
    t.sell(2_000, "a", share=0.1)  # a sell before 2x: no bundle launch candle
    t.buy(3_000, 20.0, user="b")
    t.buy(4_000, 0.1, user="c")
    t.buy(5_000, 0.1, user="d")
    t.sell(6_000, "b", share=0.5)
    t.buy(9_000, 0.1, user="e")
    return t


def conviction_engine(out=None):
    var = conviction_variant(conviction_universe(), target=0.45, stop=0.0, delay_ms=500)
    eng = Engine([var], out=out)
    eng.holder_checks = True
    asks = []
    eng.request_lookup = lambda *args: asks.append(args)
    return eng, asks


def feed(eng, tape, result, stop_ms=None):
    eng.on_create(create_row())
    looked = False
    for row in tape.rows:
        if stop_ms is not None and row["received_ms"] > stop_ms:
            return
        if not looked and row["received_ms"] > LOOKUP_MS:
            eng.on_lookup(lookup_row(result, LOOKUP_MS))
            looked = True
        eng.on_trade(row)


def buy_signals(tmp_path, result):
    eng, asks = conviction_engine(Sink(tmp_path, OUT_SCHEMAS))
    feed(eng, dump_tape(), result)
    eng.finish()
    assert len(asks) == 1 and asks[0][0] == "Mint1111"  # one lookup, asked at the gate
    eng.flush()
    decisions = read_table(tmp_path, "decisions")
    if decisions is None:
        return [], eng
    buys = decisions.filter(decisions["kind"] == "signal_buy")
    return list(zip(buys["ts_ms"].to_list(), buys["reason"].to_list(), strict=True)), eng


def test_very_high_conviction_buys_at_once(tmp_path):
    signals, _ = buy_signals(tmp_path, with_narrative(5))
    assert signals == [(5_000, "conviction 90 very_high")]
    gates = read_table(tmp_path, "gates").filter(passed=True)
    features = json.loads(gates["features"][0])
    assert features["conviction"]["tier"] == "very_high"
    assert features["narrative"]["score"] == 5 and features["x_kind"] == "community"


def test_average_conviction_waits_for_a_40pct_dip(tmp_path):
    signals, _ = buy_signals(tmp_path, with_narrative(2))
    assert signals == [(6_000, "conviction 60 average")]


def test_hard_block_never_buys(tmp_path):
    bundle = {**BUNDLE, "narrative": {"score": 5, "theme": "t", "red_flags": []}}
    signals, eng = buy_signals(tmp_path, bundle)
    assert signals == [] and eng.convictions["Mint1111"] is None
    assert not read_table(tmp_path, "gates")["passed"].any()


def test_no_lookup_no_conviction_trades():
    eng, _ = conviction_engine()
    eng.holder_checks = False  # a recording without lookups
    feed(eng, dump_tape(), with_narrative(5), stop_ms=0)
    for row in dump_tape().rows:
        eng.on_trade(row)
    eng.finish()
    assert eng.results == []


def test_mark_to_market():
    eng, _ = conviction_engine()
    eng.on_create(create_row())
    flat = eng.summary()
    assert flat["equity_lamports"] == flat["final_balance_lamports"]
    assert set(flat["open_value_lamports"].values()) == {0}

    eng, _ = conviction_engine()
    feed(eng, dump_tape(), with_narrative(5), stop_ms=5_000)
    # Any later event lands the buy decided at 5 s; a lookup for another token will do.
    eng.on_lookup(lookup_row(None, 5_600, error="busy") | {"mint": "other"})
    (slot,) = [s for s in eng.slots.values() if s.position is not None]
    curve, balance = slot.curve, list(eng.balance)
    s1, s2 = eng.summary(), eng.summary()
    assert s1 == s2 and slot.curve == curve and eng.balance == balance  # nothing moved
    (name,) = s1["equity_lamports"]
    value = s1["open_value_lamports"][name]
    assert 0 < value < slot.position.cost_lamports  # costs make a round trip lose
    assert s1["equity_lamports"][name] == s1["final_balance_lamports"][name] + value


def test_replay_with_recorded_narrative_matches_live(tmp_path):
    t, result = dump_tape(), with_narrative(5)
    sink = Sink(tmp_path, EVENT_SCHEMAS)
    sink.add("create", [create_row()])
    sink.add("trade", t.rows)
    sink.add("lookups", [lookup_row(result, LOOKUP_MS)])
    sink.flush()

    live, _ = conviction_engine()
    feed(live, t, result)
    live.finish()
    replayed, _ = conviction_engine()
    replayed.holder_checks = False  # replay turns it on: the run recorded lookups
    run_replay(replayed, tmp_path)
    assert [r for _, _, r in replayed.results] == [r for _, _, r in live.results]
    ((_, _, trade),) = live.results
    assert trade.entry_reason == "conviction 90 very_high"
    assert replayed.summary()["equity_lamports"] == live.summary()["equity_lamports"]
