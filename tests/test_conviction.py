import json

import pytest

from cryptobot.conviction import MAX_AGE_MS, blocks, conviction
from cryptobot.features import TokenState
from cryptobot.paper import EVENT_SCHEMAS, OUT_SCHEMAS, Engine, read_table, run_replay
from cryptobot.recorder import Sink
from cryptobot.v1 import conviction_universe, conviction_variant
from tests.test_holders import BUNDLE, CLEAN, lookup_row
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


BASE = 71  # clean_token with CLEAN: see test_base_score_parts


def test_base_score_parts():
    c = conviction(clean_token(), CLEAN, 4_000, NOW)
    assert (c.score, c.tier, c.drop) == (BASE, "good", 0.25)
    assert c.reasons["not_copycat"] == 20 and c.reasons["creator_record"] == 0
    assert c.reasons["x_link"] == 4 and c.reasons["climb"] == 8
    assert sum(c.reasons.values()) == pytest.approx(70.62, abs=0.01)


@pytest.mark.parametrize(
    ("graduates", "recent", "copycat", "score", "tier"),
    [
        (1, False, False, BASE + 15, "very_high"),  # earlier graduate: +15
        (0, True, False, BASE - 10, "average"),  # serial launcher: -10
        (2, True, False, BASE + 5, "good"),
        (None, None, True, BASE - 30, "skip"),  # copycat: no 20 points and a -10 flag
    ],
)
def test_creator_record_and_copycat_move_the_tier(graduates, recent, copycat, score, tier):
    tok = clean_token()
    tok.creator_graduates, tok.creator_recent_launch, tok.copycat = graduates, recent, copycat
    c = conviction(tok, CLEAN, 4_000, NOW)
    assert (c.score, c.tier) == (score, tier)


def test_narrative_is_logged_but_never_scored():
    tok = clean_token()
    plain = conviction(tok, CLEAN, 4_000, NOW)
    for score, flags in ((5, []), (1, ["no_story", "scam_signs"])):
        c = conviction(tok, with_narrative(score, flags), 4_000, NOW)
        assert (c.score, c.reasons) == (plain.score, plain.reasons)
        assert c.shadow == {
            "narrative_score": score,
            "narrative_theme": "t",
            "narrative_red_flags": flags,
        }
    assert plain.shadow["narrative_score"] is None


def test_early_buyers_holding_much_of_the_supply_lose_points():
    tok = clean_token()
    whale = tok.early_buyers[0]
    tok.balances[whale] = int(0.40 * 10**15)  # 40% of supply
    assert conviction(tok, CLEAN, 4_000, NOW).reasons["early_buyers"] == 0


def test_copycat_loses_points_and_counts_as_a_red_flag():
    tok = clean_token()
    tok.copycat = True
    c = conviction(tok, with_narrative(5, ["copycat"]), 4_000, NOW)
    assert c.red_flags == ("copycat",)
    assert c.reasons["not_copycat"] == 0 and c.reasons["red_flags"] == -10


def test_dev_and_insider_points_scale_down():
    tok = clean_token()
    tok.balances["dev"] = int(0.10 * 10**15)  # 10% of supply (6 decimals)
    c = conviction(tok, CLEAN, 4_000, NOW)
    assert c.reasons["dev_share"] == pytest.approx(4.0)  # halfway from 5% to 15%
    # the insider share counts the dev: 10% is still under the 20% line
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


def test_good_conviction_buys_the_dip_whatever_the_narrative(tmp_path):
    # At 5 s one early buyer holds 44% of supply (score 63, average: wait for 40%); at 6 s
    # it sells half, a 43% drop, and the score is 71 (good, which takes a 25% dip).
    for score in (5, 1):
        signals, _ = buy_signals(tmp_path / str(score), with_narrative(score))
        assert signals == [(6_000, "conviction 71 good")]
    gates = read_table(tmp_path / "5", "gates").filter(passed=True)
    features = json.loads(gates["features"][0])
    assert features["conviction"]["shadow"]["narrative_score"] == 5
    assert features["narrative"]["score"] == 5 and features["x_kind"] == "community"


def creators_row(creator="dev", graduated=1, last_launch_ms=-10 * 24 * 3_600_000):
    return {
        "seq": 0,
        "received_ms": 0,
        "event_index": 0,
        "list_mtime_ms": 1,
        "creators": [creator],
        "launches": [3],
        "graduated": [graduated],
        "last_launch_ms": [last_launch_ms],
    }


@pytest.mark.parametrize(
    ("row", "signal"),
    [
        (creators_row(), (6_000, "conviction 86 very_high")),  # an earlier graduate
        (creators_row(graduated=0, last_launch_ms=-3_600_000), (6_000, "conviction 61 average")),
    ],
)
def test_creator_record_changes_the_entry(tmp_path, row, signal):
    eng, _ = conviction_engine(Sink(tmp_path, OUT_SCHEMAS))
    eng.on_creators(row)
    feed(eng, dump_tape(), CLEAN)
    eng.finish()
    eng.flush()
    buys = read_table(tmp_path, "decisions").filter(kind="signal_buy")
    assert list(zip(buys["ts_ms"], buys["reason"], strict=True)) == [signal]


def test_creator_stamps():
    eng, _ = conviction_engine()
    eng.on_create(create_row(mint="m1", creator="x", ts=1_000))
    assert eng.tokens["m1"].creator_graduates is None  # no creator file yet
    assert eng.tokens["m1"].creator_recent_launch is None
    eng.on_create(create_row(mint="m2", creator="x", ts=2_000))
    assert eng.tokens["m2"].creator_recent_launch is True  # seen launching 1 s earlier
    eng.on_creators(creators_row(creator="y", graduated=2) | {"received_ms": 3_000})
    eng.on_create(create_row(mint="m3", creator="y", ts=4_000))
    eng.on_create(create_row(mint="m4", creator="z", ts=5_000))
    assert (eng.tokens["m3"].creator_graduates, eng.tokens["m3"].creator_recent_launch) == (
        2,
        False,
    )
    assert eng.tokens["m4"].creator_graduates == 0  # file loaded, creator not in it


def test_launches_older_than_the_recent_window_are_forgotten():
    from cryptobot.creators import RECENT_LAUNCH_MS

    eng, _ = conviction_engine()
    eng.on_create(create_row(mint="m1", creator="old", ts=1_000))
    eng.on_create(create_row(mint="m2", creator="new", ts=RECENT_LAUNCH_MS + 2_000))
    assert set(eng.creator_launches) == {"new"}


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
    feed(eng, dump_tape(), with_narrative(5), stop_ms=6_000)
    # Any later event lands the buy decided at 6 s; a lookup for another token will do.
    eng.on_lookup(lookup_row(None, 6_600, error="busy") | {"mint": "other"})
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
    assert trade.entry_reason == "conviction 71 good"
    assert replayed.summary()["equity_lamports"] == live.summary()["equity_lamports"]


def test_report_shows_v1_as_stated_tiers_and_spot_checks(tmp_path):
    from cryptobot.m6_report import report
    from cryptobot.v1 import CORE, universes, variant

    events = tmp_path / "events"
    sink = Sink(events, EVENT_SCHEMAS)
    name = "Evil | [click](https://x.example) **coin** with a very long name indeed"
    sink.add("create", [create_row(name=name, symbol="EVL|")])
    sink.add("trade", [dict(r, timestamp=r["received_ms"] // 1000) for r in dump_tape().rows])
    result = {**with_narrative(5), "x_link": "https://x.com/i/communities/9"}
    verdict = {"narrative_update": True, "narrative": {"score": 5, "theme": "t", "red_flags": []}}
    sink.add("lookups", [lookup_row(result, LOOKUP_MS), lookup_row(verdict, LOOKUP_MS + 1)])
    sink.flush()

    run = tmp_path / "run"
    run.mkdir()
    from cryptobot.v1 import control_universe, labels_universe

    conv = conviction_variant(conviction_universe(), target=0.45, stop=0.0)
    control = conviction_variant(control_universe(), target=0.45, stop=0.0)
    labels = conviction_variant(labels_universe(), 0.45, 0.0, shadow=True)
    variants = [variant(u, *CORE) for u in universes()[:2]] + [conv, control, labels]
    (run / "variants.json").write_text(json.dumps([v.describe() for v in variants]))
    eng = Engine(variants, out=Sink(run, OUT_SCHEMAS))
    eng.summary_path = run / "summary.json"
    eng.summary_extra = {"mode": "replay", "source": str(events), "running": False}
    run_replay(eng, events)
    text = "\n".join(report(run))
    assert text.index("## V1 as stated") < text.index("## Run") < text.index("## Experiments")
    assert "| new_pairs | dip40\\|tp45\\|sl0\\|d2s | 1 |" in text
    # Shown even with no trades: Final Stretch takes none in a run's first 40 minutes.
    assert "| final_stretch_50 | dip40\\|tp45\\|sl0\\|d2s | 0 | n/a" in text
    assert "| conviction | tp45\\|sl0\\|d2s | 1 |" in text
    assert "| conviction | good | 1 |" in text
    assert "| conviction | very_high | 0 | n/a | n/a |" in text
    chance = text[text.index("## Picks vs chance") : text.index("## Conviction by tier")]
    assert "| conviction | 1 |" in chance and "| control_random | 1 |" in chance
    assert "| labels | 1 |" in chance
    assert "labels|" not in text[: text.index("## Picks vs chance")]  # not a wallet
    assert "| control_random | random | 1 |" in text
    narrative = text[text.index("## Conviction by narrative score") :]
    assert "| 5 | 1 | 0.0%" in narrative and "| 1 | 0 | n/a | n/a |" in narrative
    spot = text[text.index("## Spot-check list") :]
    row = next(ln for ln in spot.splitlines() if "| conviction\\|tp45" in ln)
    assert "| `Evil \\| [click](https://x.example) **coin` (`EVL\\|`) |" in row
    assert "community `https://x.com/i/communities/9`" in row
    assert "| 71 good | 5/5 `t` |" in row
    assert "| final_stretch_50 | n/a | n/a | n/a | n/a |" in text  # no network-cost data
    summary = json.loads((run / "summary.json").read_text())
    assert summary["equity_lamports"] == summary["final_balance_lamports"]  # all closed


def _gated(score_or_none, base_tier_result=CLEAN, tok=None, judged=True):
    from cryptobot.conviction import narrative_gate

    conv = conviction(tok or clean_token(), base_tier_result, 4_000, NOW)
    result = dict(base_tier_result)
    if judged:
        result["narrative_done"] = True
        if score_or_none is not None:
            result["narrative"] = {"score": score_or_none, "theme": "t", "red_flags": []}
    return narrative_gate(conv, result)


@pytest.mark.parametrize(
    ("score", "tier", "drop"),
    [(1, "skip", None), (2, "skip", None), (3, "average", 0.40), (4, "good", 0.25)],
)
def test_narrative_gate_caps_a_good_token(score, tier, drop):
    c = _gated(score)
    assert (c.score, c.tier, c.drop) == (BASE, tier, drop)  # the score never changes


def test_narrative_gate_waits_for_the_verdict_and_caps_an_empty_one():
    assert (_gated(None, judged=False).tier, _gated(None, judged=False).drop) == (
        "waiting_narrative",
        None,
    )
    assert _gated(None).tier == "average"  # judge failed or quota spent


def test_only_a_five_keeps_very_high():
    tok = clean_token()
    tok.creator_graduates = 1  # BASE + 15: very high
    assert _gated(5, tok=tok).tier == "very_high"
    tok2 = clean_token()
    tok2.creator_graduates = 1
    assert _gated(4, tok=tok2).tier == "good"


def test_narrative_gate_never_raises_a_skip():
    tok = clean_token()
    tok.copycat = True
    assert _gated(5, tok=tok).tier == "skip"


def gated_signals(tmp_path, score, verdict_ms=LOOKUP_MS + 100):
    var = conviction_variant(
        conviction_universe(narrative_gate=True), target=0.45, stop=0.0, delay_ms=500
    )
    eng = Engine([var], out=Sink(tmp_path, OUT_SCHEMAS))
    eng.holder_checks = True
    eng.request_lookup = lambda *args: None
    eng.on_create(create_row())
    looked = judged = False
    for row in dump_tape().rows:
        if not looked and row["received_ms"] > LOOKUP_MS:
            eng.on_lookup(lookup_row(CLEAN, LOOKUP_MS))
            looked = True
        if score is not None and not judged and row["received_ms"] > verdict_ms:
            verdict = {"narrative_update": True, "narrative": {"score": score, "red_flags": []}}
            eng.on_lookup(lookup_row(verdict, verdict_ms))
            judged = True
        eng.on_trade(row)
    eng.finish()
    eng.flush()
    decisions = read_table(tmp_path, "decisions")
    if decisions is None:
        return []
    buys = decisions.filter(decisions["kind"] == "signal_buy")
    return list(zip(buys["ts_ms"].to_list(), buys["reason"].to_list(), strict=True))


def test_gated_universe_enters_by_the_narrative_cap(tmp_path):
    assert gated_signals(tmp_path / "5", 5) == [(6_000, "conviction 71 good")]
    assert gated_signals(tmp_path / "3", 3) == [(6_000, "conviction 71 average")]
    assert gated_signals(tmp_path / "1", 1) == []
    assert gated_signals(tmp_path / "none", None) == []  # no verdict: waits, never buys


def test_control_flips_a_fixed_coin_per_mint():
    from cryptobot.conviction import control_pick

    conv = conviction(clean_token(), CLEAN, 4_000, NOW)
    picked, dropped = control_pick(conv, "Mint1111"), control_pick(conv, "Mint2222")
    assert (picked.tier, picked.drop, picked.score) == ("random", 0.25, BASE)
    assert (dropped.tier, dropped.drop) == ("skip", None)


def universe_signals(tmp_path, variant):
    eng = Engine([variant], out=Sink(tmp_path, OUT_SCHEMAS))
    eng.holder_checks = True
    eng.request_lookup = lambda *args: None
    feed(eng, dump_tape(), CLEAN)
    eng.finish()
    eng.flush()
    buys = read_table(tmp_path, "decisions").filter(kind="signal_buy")
    return list(zip(buys["ts_ms"].to_list(), buys["reason"].to_list(), strict=True)), eng


def test_control_enters_its_picks_on_a_25pct_dip(tmp_path):
    from cryptobot.v1 import control_universe

    var = conviction_variant(control_universe(), target=0.45, stop=0.0, delay_ms=500)
    signals, _ = universe_signals(tmp_path, var)
    assert signals == [(6_000, "conviction 71 random")]


def test_labels_enter_every_eligible_token_at_once_without_a_wallet(tmp_path):
    from cryptobot.v1 import labels_universe

    var = conviction_variant(labels_universe(), 0.45, 0.0, delay_ms=500, shadow=True)
    signals, eng = universe_signals(tmp_path, var)
    # The first trade after the lookup arrives (4.5 s); the score was 63 then, a skip
    # for conviction, but labels take every eligible token.
    assert len(signals) == 1 and signals[0][0] > LOOKUP_MS
    assert signals[0][1].startswith("conviction ") and signals[0][1].endswith(" label")
    assert len(eng.results) == 1
    assert eng.balance[0] == Engine([var]).balance[0]  # shadow: the wallet never moves
