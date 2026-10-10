"""Decode Pump.fun program events from transaction logs.

Field lists follow idl/pump.json in pump-fun/pump-public-docs. Events are Borsh:
an 8-byte discriminator, then the fields in order. Pump.fun appends new fields at the
end over time, so decoding stops at the first field the bytes don't cover and leaves
the rest as None. TradeEvent ends in a variable-length shareholder list; decoding
stops before it.
"""

from __future__ import annotations

import base64
import binascii
import struct

from cryptobot.chain import b58encode

CREATE_EVENT = bytes.fromhex("1b72a94ddeeb6376")
TRADE_EVENT = bytes.fromhex("bddb7fd34ee661ee")

CREATE_FIELDS = (
    ("name", "string"),
    ("symbol", "string"),
    ("uri", "string"),
    ("mint", "pubkey"),
    ("bonding_curve", "pubkey"),
    ("user", "pubkey"),
    ("creator", "pubkey"),
    ("timestamp", "i64"),
    ("virtual_token_reserves", "u64"),
    ("virtual_sol_reserves", "u64"),
    ("real_token_reserves", "u64"),
    ("token_total_supply", "u64"),
    ("token_program", "pubkey"),
    ("is_mayhem_mode", "bool"),
    ("is_cashback_enabled", "bool"),
    ("quote_mint", "pubkey"),
    ("virtual_quote_reserves", "u64"),
    ("creator_fee_bps", "u64"),
    ("is_holder_reward", "bool"),
    ("depth", "u8"),
)

TRADE_FIELDS = (
    ("mint", "pubkey"),
    ("sol_amount", "u64"),
    ("token_amount", "u64"),
    ("is_buy", "bool"),
    ("user", "pubkey"),
    ("timestamp", "i64"),
    ("virtual_sol_reserves", "u64"),
    ("virtual_token_reserves", "u64"),
    ("real_sol_reserves", "u64"),
    ("real_token_reserves", "u64"),
    ("fee_recipient", "pubkey"),
    ("fee_basis_points", "u64"),
    ("fee", "u64"),
    ("creator", "pubkey"),
    ("creator_fee_basis_points", "u64"),
    ("creator_fee", "u64"),
    ("track_volume", "bool"),
    ("total_unclaimed_tokens", "u64"),
    ("total_claimed_tokens", "u64"),
    ("current_sol_volume", "u64"),
    ("last_update_timestamp", "i64"),
    ("ix_name", "string"),
    ("mayhem_mode", "bool"),
    ("cashback_fee_basis_points", "u64"),
    ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"),
    ("buyback_fee", "u64"),
)

_FIXED = {"u64": ("<Q", 8), "i64": ("<q", 8), "u8": ("<B", 1), "bool": ("<?", 1)}


def decode_fields(data: bytes, fields: tuple[tuple[str, str], ...]) -> dict:
    out: dict = dict.fromkeys(name for name, _ in fields)
    o = 0
    for name, kind in fields:
        if kind == "pubkey":
            if o + 32 > len(data):
                break
            out[name] = b58encode(data[o : o + 32])
            o += 32
        elif kind == "string":
            if o + 4 > len(data):
                break
            (n,) = struct.unpack_from("<I", data, o)
            if o + 4 + n > len(data):
                break
            out[name] = data[o + 4 : o + 4 + n].decode("utf-8", errors="replace")
            o += 4 + n
        else:
            fmt, size = _FIXED[kind]
            if o + size > len(data):
                break
            (out[name],) = struct.unpack_from(fmt, data, o)
            o += size
    return out


def decode(data: bytes) -> tuple[str, dict] | None:
    """Return ("create" | "trade", fields), or None for any other event."""
    head, body = data[:8], data[8:]
    if head == CREATE_EVENT:
        return "create", decode_fields(body, CREATE_FIELDS)
    if head == TRADE_EVENT:
        return "trade", decode_fields(body, TRADE_FIELDS)
    return None


def events_from_logs(logs: list[str]) -> list[tuple[str, dict]]:
    """Every create and trade event in a transaction's logs, in log order."""
    found = []
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line.removeprefix("Program data: "), validate=True)
        except binascii.Error:
            continue
        event = decode(raw)
        if event:
            found.append(event)
    return found
