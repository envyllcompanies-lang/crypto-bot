"""Validation checks on synthetic trade rows built with curve.py."""

import base64
import struct

import polars as pl

from cryptobot.chain import TRADE_EVENT_DISCRIMINATOR, b58encode, decode_trade_event, trade_events
from cryptobot.curve import INITIAL_VIRTUAL_SOL, Curve, to_lamports
from cryptobot.validate import chain_continuity, curve_math_error, sol_quoted_classic

MINT = "MintAAAA"


def _rows() -> pl.DataFrame:
    """A launch, two buys and a sell, recorded the way the dataset records them."""
    rows, curve = [], Curve()
    for kind, amount in (("buy", to_lamports(1.0)), ("buy", to_lamports(0.5)), ("sell", None)):
        if kind == "buy":
            curve, tokens, spent = curve.after_buy(amount)
            sol = spent
        else:
            tokens = 10_000_000_000_000
            curve, sol = curve.after_sell(tokens)
        rows.append(
            {
                "mint": MINT,
                "ix_name": kind,
                "sol_amount": sol,
                "token_amount": tokens,
                "virtual_sol_reserves": curve.virtual_sol,
                "virtual_token_reserves": curve.virtual_tokens,
            }
        )
    return pl.DataFrame(rows)


def _creates() -> pl.DataFrame:
    return pl.DataFrame(
        {"mint": [MINT], "virtual_sol_reserves": [INITIAL_VIRTUAL_SOL], "is_mayhem_mode": [False]}
    )


def test_honest_trades_match_the_formula_and_chain_together():
    errors = curve_math_error(_rows())
    assert errors["err"].max() == 0
    assert errors["k_rel"].abs().max() < 1e-9
    assert chain_continuity(_rows(), _creates())["chained"].all()


def test_a_tampered_amount_is_caught():
    tampered = _rows().with_columns(
        token_amount=pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.col("token_amount") - 1_000_000)
        .otherwise(pl.col("token_amount"))
    )
    assert curve_math_error(tampered)["err"][1] > 1
    assert not chain_continuity(tampered, _creates())["chained"][1]


def test_classic_filter_drops_mayhem_and_non_sol_curves():
    creates = pl.DataFrame(
        {
            "mint": ["a", "b", "c"],
            "virtual_sol_reserves": [INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_SOL, 4_292_000_000],
            "is_mayhem_mode": [False, True, False],
        }
    )
    assert sol_quoted_classic(creates)["mint"].to_list() == ["a"]


def test_b58encode_matches_known_addresses():
    assert b58encode(bytes(32)) == "1" * 32
    assert b58encode(b"\x00\x00\x01") == "112"


def test_trade_event_round_trip():
    mint, user = bytes(range(32)), bytes(range(32, 64))
    payload = (
        TRADE_EVENT_DISCRIMINATOR
        + mint
        + struct.pack("<QQ", 123, 456)
        + b"\x01"
        + user
        + struct.pack("<qQQ", 1_789_000_000, 40_000_000_000, 800_000_000_000_000)
    )
    event = decode_trade_event(payload)
    assert event is not None
    assert (event.mint, event.user) == (b58encode(mint), b58encode(user))
    assert (event.sol_amount, event.token_amount, event.is_buy) == (123, 456, True)
    assert event.virtual_sol_reserves == 40_000_000_000
    logs = ["Program log: hi", "Program data: " + base64.b64encode(payload).decode()]
    assert trade_events(logs) == [event]


def test_other_events_are_ignored():
    assert decode_trade_event(b"\x00" * 200) is None
