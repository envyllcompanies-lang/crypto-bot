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
