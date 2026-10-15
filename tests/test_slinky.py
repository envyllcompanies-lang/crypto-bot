"""Slinky21 rows to recorder-shaped events, in chain order."""

from datetime import UTC, datetime

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS
from cryptobot.features import TokenState
from cryptobot.slinky import chain_order, events


def trade(sol, tok, vsol, vtok, buy=True, mint="m"):
    return {
        "_kind": "trade",
        "mint": mint,
        "is_buy": buy,
        "sol_amount": sol,
        "token_amount": tok,
        "virtual_sol_reserves": vsol,
        "virtual_token_reserves": vtok,
    }


def test_two_buys_listed_out_of_order_are_put_back_in_chain_order():
    # From the 7 Oct 2026 file: one transaction, two buys, listed second-first.
    first = trade(1396550213, 45587768711485, 32108564671, 1002536249498696)
    second = trade(1379203753, 41289755093831, 33487768424, 961246494404865)
    assert chain_order([second, first]) == [first, second]


def test_other_tokens_keep_their_places_and_unchainable_groups_stay_as_listed():
    a1 = trade(10, 10, 110, 990, mint="a")
    a2 = trade(10, 10, 120, 980, mint="a")
    b = trade(5, 5, 105, 995, mint="b")
    assert chain_order([a2, b, a1]) == [a1, b, a2]
    broken = [trade(1, 1, 500, 500), trade(1, 1, 900, 900)]
    assert chain_order(broken) == broken


def _write(root, stamp, creates, trades):
    base = root / "p01" / "2026-10_d01-12"
    for name, rows in (("create", creates), ("trade", trades)):
        (base / name).mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(base / name / f"{name}_{stamp}.parquet")


def _t(ms):
    return datetime.fromtimestamp(ms / 1000, UTC)


def test_events_convert_order_and_drop_non_sol_curves(tmp_path):
    vsol, vtok = INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS
    create = {
        "block_time": _t(1_000),
        "slot": 10,
        "tx_index": 1,
        "signature": "c",
        "mint": "m",
        "bonding_curve": "bc",
        "creator": "dev",
        "tx_signer": "dev",
        "token_name": "Coin",
        "symbol": "C",
        "uri": "u",
        "token_program": "tp",
        "is_mayhem_mode": False,
        "token_total_supply": 10**15,
        "real_token_reserves": 793_100_000_000_000,
        "virtual_sol_reserves": vsol,
        "virtual_token_reserves": vtok,
        "ingested_at": _t(1_500),
    }
    sol = 1_000_000_000
    after = TokenState.from_create({**create, "received_ms": 0, "name": "", "symbol": ""})
    curve, tokens, _ = after.curve.after_buy(sol)

    def tr(slot, ingested, user, sol_amt, tok, vs, vt, ix="buy"):
        return {
            "block_time": _t(2_000),
            "slot": slot,
            "tx_index": 0,
            "signature": user,
            "mint": "m",
            "trade_user": user,
            "creator": "dev",
            "ix_name": ix,
            "sol_amount": sol_amt,
            "token_amount": tok,
            "virtual_sol_reserves": vs,
            "virtual_token_reserves": vt,
            "real_sol_reserves": 0,
            "real_token_reserves": 0,
            "fee": 0,
            "creator_fee": 0,
            "ingested_at": _t(ingested),
        }

    trades = [
        tr(12, 2_400, "late", 0, 1, 0, 1, ix="sell"),  # non-SOL curve: zero reserves
        tr(11, 2_600, "buyer", sol, tokens, curve.virtual_sol, curve.virtual_tokens),
    ]
    _write(tmp_path, "20261001T000000", [create], trades)
    got = list(events(tmp_path))
    assert [k for k, _ in got] == ["create", "trade"]
    (_, c), (_, t) = got
    assert c["name"] == "Coin" and c["user"] == "dev" and c["received_ms"] == 1_500
    assert t["user"] == "buyer" and t["is_buy"] is True and t["received_ms"] == 2_600
    assert t["seq"] == 1 and t["timestamp"] == 2
    tok = TokenState.from_create(c)
    tok.apply_trade(t)
    assert not tok.tainted
    ((kind, only),) = events(tmp_path, start=_t(1_800))  # the create is before the start
    assert kind == "trade" and {**only, "seq": 1} == t


def test_parallel_backtest_parts_merge_into_one_run(tmp_path):
    import json

    from cryptobot.backtest import run_parallel

    vsol, vtok = INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS

    def create(mint, ms):
        return {
            "block_time": _t(ms),
            "slot": ms // 400,
            "tx_index": 0,
            "signature": mint,
            "mint": mint,
            "bonding_curve": "bc",
            "creator": "dev",
            "tx_signer": "dev",
            "token_name": mint,
            "symbol": mint,
            "uri": "u",
            "token_program": "tp",
            "is_mayhem_mode": False,
            "token_total_supply": 10**15,
            "real_token_reserves": 793_100_000_000_000,
            "virtual_sol_reserves": vsol,
            "virtual_token_reserves": vtok,
            "ingested_at": _t(ms),
        }

    trade = {
        "block_time": _t(1_000),
        "slot": 2,
        "tx_index": 1,
        "signature": "s",
        "mint": "zz",
        "trade_user": "u",
        "creator": "dev",
        "ix_name": "sell",
        "sol_amount": 0,
        "token_amount": 1,
        "virtual_sol_reserves": 0,  # non-SOL curve: dropped
        "virtual_token_reserves": 1,
        "real_sol_reserves": 0,
        "real_token_reserves": 0,
        "fee": 0,
        "creator_fee": 0,
        "ingested_at": _t(1_000),
    }
    day = 86_400_000
    _write(tmp_path / "raw", "19700101T120000", [create("a", 1_000)], [trade])
    _write(tmp_path / "raw", "19700102T120000", [create("b", day + 1)], [trade])
    out = tmp_path / "bt"
    merged = run_parallel(out, _t(0), _t(2 * day), 2, root=tmp_path / "raw")
    assert merged["parts"] == 2 and merged["stats"]["creates"] == 2  # one launch per part
    assert json.loads((out / "summary.json").read_text())["running"] is False
    assert (out / "variants.json").exists() and not (out / "parts").exists()
