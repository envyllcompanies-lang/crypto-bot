import base64
import struct

from cryptobot.chain import b58encode
from cryptobot.events import (
    CREATE_EVENT,
    CREATE_FIELDS,
    TRADE_EVENT,
    TRADE_FIELDS,
    decode,
    events_from_logs,
)

KEY = bytes(range(32))
FMT = {"u64": "<Q", "i64": "<q", "u8": "<B", "bool": "<?"}


def pack(fields, values):
    out = b""
    for name, kind in fields:
        v = values[name]
        if kind == "pubkey":
            out += v
        elif kind == "string":
            raw = v.encode()
            out += struct.pack("<I", len(raw)) + raw
        else:
            out += struct.pack(FMT[kind], v)
    return out


def sample(fields):
    vals = {}
    for i, (name, kind) in enumerate(fields):
        vals[name] = {"pubkey": KEY, "string": f"s{i}", "bool": True}.get(kind, i + 1)
    return vals


def test_create_event_round_trip():
    vals = sample(CREATE_FIELDS)
    kind, got = decode(CREATE_EVENT + pack(CREATE_FIELDS, vals))
    assert kind == "create"
    assert got["mint"] == b58encode(KEY)
    assert got["name"] == vals["name"]
    assert got["token_total_supply"] == vals["token_total_supply"]
    assert got["is_mayhem_mode"] is True
    assert got["depth"] == vals["depth"]


def test_trade_event_ignores_trailing_shareholder_list():
    vals = sample(TRADE_FIELDS)
    data = TRADE_EVENT + pack(TRADE_FIELDS, vals) + b"\x05\x00\x00\x00garbage"
    kind, got = decode(data)
    assert kind == "trade"
    assert got["sol_amount"] == vals["sol_amount"]
    assert got["ix_name"] == vals["ix_name"]
    assert got["buyback_fee"] == vals["buyback_fee"]


def test_older_shorter_event_leaves_missing_fields_none():
    vals = sample(TRADE_FIELDS)
    short = pack(TRADE_FIELDS[:8], vals)
    _, got = decode(TRADE_EVENT + short)
    assert got["virtual_token_reserves"] == vals["virtual_token_reserves"]
    assert got["real_sol_reserves"] is None
    assert got["ix_name"] is None


def test_unknown_discriminator_is_skipped():
    assert decode(b"\x00" * 64) is None


def test_events_from_logs_reads_only_program_data_lines():
    trade = TRADE_EVENT + pack(TRADE_FIELDS, sample(TRADE_FIELDS))
    logs = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        "Program log: Instruction: Buy",
        "Program data: " + base64.b64encode(trade).decode(),
        "Program data: not*base64",
        "Program data: " + base64.b64encode(b"\x01" * 40).decode(),
    ]
    found = events_from_logs(logs)
    assert [k for k, _ in found] == ["trade"]
