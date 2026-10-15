import base64

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_TOKENS, LAUNCH, to_lamports
from cryptobot.events import CREATE_EVENT, CREATE_FIELDS, TRADE_EVENT, TRADE_FIELDS
from cryptobot.m5_report import report
from cryptobot.recorder import Sink, rows_from_notification
from tests.test_events import pack, sample


def note(logs, err=None, sig="sig1", slot=7):
    return {
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": slot},
                "value": {"signature": sig, "err": err, "logs": logs},
            }
        },
    }


def line(data):
    return "Program data: " + base64.b64encode(data).decode()


def create_bytes():
    vals = sample(CREATE_FIELDS)
    vals.update(
        virtual_sol_reserves=LAUNCH.virtual_sol,
        virtual_token_reserves=INITIAL_VIRTUAL_TOKENS,
        token_total_supply=10**15,
        is_mayhem_mode=False,
    )
    return CREATE_EVENT + pack(CREATE_FIELDS, vals)


def trade_bytes(sol, tok, is_buy, vsol, vtok, ix):
    vals = sample(TRADE_FIELDS)
    vals.update(
        sol_amount=sol,
        token_amount=tok,
        is_buy=is_buy,
        virtual_sol_reserves=vsol,
        virtual_token_reserves=vtok,
        ix_name=ix,
        fee_basis_points=95,
        creator_fee_basis_points=30,
    )
    return TRADE_EVENT + pack(TRADE_FIELDS, vals)


def test_rows_split_by_kind_with_metadata():
    creates, trades, truncated = rows_from_notification(
        note([line(create_bytes()), "Log truncated"]), received_ms=123
    )
    assert len(creates) == 1 and trades == []
    assert truncated
    row = creates[0]
    assert (row["signature"], row["slot"], row["received_ms"], row["event_index"]) == (
        "sig1",
        7,
        123,
        0,
    )


def test_failed_transaction_yields_no_rows():
    msg = note([line(create_bytes())], err={"InstructionError": [0, "Custom"]})
    assert rows_from_notification(msg, 0) == ([], [], False)


def toy_capture(tmp_path):
    """One classic launch, a 1 SOL buy and a full sell, priced with curve.py."""
    after_buy, tokens, spent = LAUNCH.after_buy(to_lamports(1.0))
    after_sell, sol_out = after_buy.after_sell(tokens)
    sink = Sink(tmp_path)
    for i, logs in enumerate(
        [
            [line(create_bytes())],
            [
                line(
                    trade_bytes(
                        spent, tokens, True, after_buy.virtual_sol, after_buy.virtual_tokens, "buy"
                    )
                )
            ],
            [
                line(
                    trade_bytes(
                        sol_out,
                        tokens,
                        False,
                        after_sell.virtual_sol,
                        after_sell.virtual_tokens,
                        "sell",
                    )
                )
            ],
        ]
    ):
        creates, trades, _ = rows_from_notification(note(logs, sig=f"s{i}"), 1_000 + i)
        sink.add("create", creates)
        sink.add("trade", trades)
    sink.add(
        "sessions",
        [
            {
                "connected_ms": 0,
                "disconnected_ms": 60_000,
                "txs": 3,
                "failed_txs": 0,
                "truncated_txs": 0,
                "end_reason": "deadline",
            }
        ],
    )
    sink.flush()
    return {
        t: pl.read_parquet(tmp_path / t / "**" / "*.parquet")
        for t in ("create", "trade", "sessions")
    }


def test_sink_round_trip_keeps_schema(tmp_path):
    data = toy_capture(tmp_path)
    assert data["trade"].height == 2
    assert data["trade"]["sol_amount"].dtype == pl.UInt64
    assert data["create"]["mint"][0] == data["trade"]["mint"][0]


def test_report_passes_on_curve_consistent_toy_token(tmp_path):
    text = "\n".join(report(toy_capture(tmp_path)))
    assert "| sell | 1 | 100.00% | 100.00% | 0 |" in text
    assert "continues a known state: 100.00%" in text
    assert "| 95 | 30 | 2 |" in text
