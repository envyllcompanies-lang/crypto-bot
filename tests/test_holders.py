import json

from cryptobot.holders import (
    balances_alike,
    classify,
    funder_of,
    funding_clustered,
    lookup,
    x_link_kind,
)
from cryptobot.paper import EVENT_SCHEMAS, Engine, run_replay
from cryptobot.recorder import Sink
from cryptobot.v1 import universes, variant
from tests.test_paper import MINT, Tape, create_row

SOL = 10**9
CLEAN = {
    "wallets": ["b", "a"],
    "balances": [SOL, 1_000 * SOL],
    "first_ms": [None, None],
    "funders": [None, None],
    "x_kind": "community",
}
BUNDLE = {
    "wallets": list("vwxyz"),
    "balances": [int(x * SOL) for x in (0.02, 0.1, 0.03, 0.1, 5.0)],
    "first_ms": [None] * 5,
    "funders": ["f"] * 5,
    "x_kind": "community",
}


def test_balances_alike_matches_v1_example():
    assert balances_alike(BUNDLE["balances"])
    assert not balances_alike([int(x * SOL) for x in (0.01, 1, 10, 50, 200)])
    assert not balances_alike([SOL, SOL])  # too few holders to call alike
    big_traders = [int(x * SOL) for x in (6.5, 7.6, 9.8, 9.3, 25.5)]
    assert not balances_alike(big_traders)


def test_funding_clustered():
    now = 10_000_000
    assert funding_clustered(["f", "f", "f", None, "g"], [None] * 5, now)
    assert funding_clustered([None] * 5, [now - 60_000] * 3 + [None, None], now)
    assert not funding_clustered(list("abcde"), [0] * 5, now)


def test_funder_of():
    paid = {
        "transaction": {"message": {"accountKeys": ["payer", "w"]}},
        "meta": {"preBalances": [5, 0], "postBalances": [3, 2]},
    }
    assert funder_of(paid, "w") == "payer"
    self_paid = {
        "transaction": {"message": {"accountKeys": ["w", "src"]}},
        "meta": {"preBalances": [1, 9], "postBalances": [8, 2]},
    }
    assert funder_of(self_paid, "w") == "src"
    assert funder_of(None, "w") is None


def test_x_link_kind():
    assert x_link_kind("https://x.com/i/communities/123") == "community"
    assert x_link_kind("https://twitter.com/abc/status/1") == "tweet"
    assert x_link_kind("x.com/abc") == "profile"
    assert x_link_kind("https://t.me/abc") is None
    assert x_link_kind(None) is None


def test_lookup_with_canned_rpc_and_metadata():
    def call(method, params):
        if method == "getMultipleAccounts":
            return {"value": [{"lamports": 7}, None]}
        if method == "getSignaturesForAddress":
            return [{"signature": "s2", "blockTime": 200}, {"signature": "s1", "blockTime": 100}]
        return {"transaction": {"message": {"accountKeys": ["funder", "x"]}}, "meta": {}}

    meta = {"twitter": "https://x.com/i/communities/9", "telegram": "https://t.me/x"}
    res = lookup(["w1", "w2"], "https://meta.example", call, fetch=lambda uri: meta)
    assert res["balances"] == [7, 0]
    assert res["first_ms"] == [100_000, 100_000]
    assert res["funders"] == ["funder", "funder"]
    assert res["x_kind"] == "community" and res["telegram"] == "https://t.me/x"
    assert classify(res, now_ms=100_001) == {
        "x_link_is_community": True,
        "top5_sol_balances_differ": True,
        "top5_funding_not_clustered": True,
    }

    def broken(uri):
        raise OSError("unreachable")

    res = lookup([], "https://meta.example", call, fetch=broken)
    assert classify(res, 0)["x_link_is_community"] is None


def passing_new_pairs_tape():
    t = Tape()
    t.buy(1_000, 1.0, user="a")
    t.sell(2_000, "a", share=0.1)  # a sell before 2x: no bundle launch candle
    t.buy(3_000, 8.0, user="b")  # now above V1's $7K New Pairs minimum
    t.buy(4_000, 0.1, user="c")
    t.buy(5_000, 0.1, user="d")
    return t


def new_pairs_engine():
    var = variant(universes()[0], drop=0.0, target=0.45, stop=0.0, delay_ms=500)
    eng = Engine([var])
    eng.holder_checks = True
    asks = []
    eng.request_lookup = lambda mint, wallets, uri, ts: asks.append((mint, wallets, ts))
    return eng, asks


def lookup_row(result, ts, error=None):
    return {
        "seq": 0,
        "received_ms": ts,
        "event_index": 0,
        "mint": MINT,
        "requested_ms": ts,
        "result": None if result is None else json.dumps(result),
        "error": error,
    }


def run_until_lookup(result, error=None):
    t = passing_new_pairs_tape()
    eng, asks = new_pairs_engine()
    eng.on_create(create_row())
    for row in t.rows[:4]:
        eng.on_trade(row)
    assert len(asks) == 1 and asks[0][1][0] == "b"  # asked once, largest holder first
    assert not any(s.pending or s.position for s in eng.slots.values())  # waits for it
    eng.on_lookup(lookup_row(result, 4_500, error))
    eng.on_trade(t.rows[4])
    return any(s.pending for s in eng.slots.values())


def test_entry_waits_for_lookup_then_enters_on_clean_holders():
    assert run_until_lookup(CLEAN)


def test_bundled_holders_block_entry():
    assert not run_until_lookup(BUNDLE)


def test_failed_lookup_counts_as_not_evaluated():
    assert run_until_lookup(None, error="HTTPError")


def test_recorded_lookups_replay_and_switch_checks_on(tmp_path):
    t = passing_new_pairs_tape()
    sink = Sink(tmp_path, EVENT_SCHEMAS)
    sink.add("create", [create_row()])
    sink.add("trade", t.rows)
    sink.add("lookups", [lookup_row(CLEAN, 4_500)])
    sink.flush()
    eng, _ = new_pairs_engine()
    eng.holder_checks = False  # replay turns it on because the run recorded lookups
    run_replay(eng, tmp_path)
    assert eng.holder_checks and eng.stats["lookups_done"] == 1
    assert len(eng.results) == 1  # entered after the lookup; valued at the end
