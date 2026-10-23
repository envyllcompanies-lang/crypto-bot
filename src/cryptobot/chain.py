"""Check dataset trade rows against the chain through a public Solana RPC.

For each sampled signature, fetch the transaction, find Pump.fun's TradeEvent in
its logs ("Program data:" lines), and compare the event's amounts and reserves
with the dataset row. Read-only: nothing here signs or sends a transaction.
"""

from __future__ import annotations

import base64
import json
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Anchor event discriminator: sha256("event:TradeEvent")[:8].
TRADE_EVENT_DISCRIMINATOR = bytes.fromhex("bddb7fd34ee661ee")


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


@dataclass(frozen=True)
class TradeEvent:
    mint: str
    sol_amount: int
    token_amount: int
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol_reserves: int
    virtual_token_reserves: int


def decode_trade_event(data: bytes) -> TradeEvent | None:
    """Decode the leading fields of a TradeEvent; None if the bytes aren't one."""
    if len(data) < 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8:
        return None
    if data[:8] != TRADE_EVENT_DISCRIMINATOR:
        return None
    o = 8
    mint = b58encode(data[o : o + 32])
    o += 32
    sol, tok = struct.unpack_from("<QQ", data, o)
    o += 16
    is_buy = data[o] == 1
    o += 1
    user = b58encode(data[o : o + 32])
    o += 32
    ts, vsol, vtok = struct.unpack_from("<qQQ", data, o)
    return TradeEvent(mint, sol, tok, is_buy, user, ts, vsol, vtok)


def trade_events(logs: list[str]) -> list[TradeEvent]:
    events = []
    for line in logs:
        if line.startswith("Program data: "):
            event = decode_trade_event(base64.b64decode(line.removeprefix("Program data: ")))
            if event:
                events.append(event)
    return events


def get_transaction(signature: str, rpc: str = PUBLIC_RPC, retries: int = 6) -> dict | None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        }
    ).encode()
    delay = 2.0
    for _ in range(retries):
        req = urllib.request.Request(rpc, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp).get("result")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(delay)
            delay *= 2
    return None


def check_row(row: dict, tx: dict | None) -> dict:
    """Compare one dataset trade row with its on-chain transaction."""
    out = {"signature": row["signature"], "found": tx is not None}
    if tx is None:
        return out
    out["slot_match"] = tx["slot"] == row["slot"]
    out["time_match"] = tx["blockTime"] == int(row["block_time"].timestamp())
    out["tx_ok"] = tx["meta"]["err"] is None
    events = [e for e in trade_events(tx["meta"]["logMessages"] or []) if e.mint == row["mint"]]
    out["event_found"] = bool(events)
    out["fields_match"] = any(
        e.sol_amount == row["sol_amount"]
        and e.token_amount == row["token_amount"]
        and e.user == row["trade_user"]
        and e.virtual_sol_reserves == row["virtual_sol_reserves"]
        and e.virtual_token_reserves == row["virtual_token_reserves"]
        for e in events
    )
    return out
