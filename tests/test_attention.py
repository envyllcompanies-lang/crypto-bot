"""Wikipedia attention matching."""

from datetime import date

import polars as pl

import cryptobot.attention as att


def test_keys_use_the_title_and_a_short_title_s_last_word():
    assert att.keys("Christa_Pike") == {"christapike", "pike"}
    assert att.keys("Jimothy_(raccoon)") == {"jimothy"}
    assert att.keys("A_B") == set()  # too short
    assert att.norm("Pokémon!") == "pokemon"


def test_matches_flags_the_first_coin_on_a_topic(monkeypatch):
    monkeypatch.setattr(att, "day_keys", lambda d: {"pike": 2} if d == date(2026, 10, 1) else {})
    creates = pl.DataFrame(
        {
            "mint": ["a", "b", "c"],
            "name": ["Pike", "PIKE coin", "Other"],
            "symbol": ["PK", "PIKE", "OTH"],
            "day": [date(2026, 10, 1)] * 3,
            "created_ms": [1, 2, 3],
        }
    )
    m = att.matches(creates).sort("mint")
    assert m["wiki_match"].to_list() == [True, True, False]
    assert m["wiki_first"].to_list() == [True, False, None]
