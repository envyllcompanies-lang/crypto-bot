"""Social signal parsing, state and matching."""

import json

from cryptobot import social


def test_parsers_keep_solana_mints_and_whole_search_phrases():
    dex = json.dumps(
        [
            {"chainId": "solana", "tokenAddress": "MintA", "totalAmount": 30},
            {"chainId": "base", "tokenAddress": "0xabc", "totalAmount": 10},
        ]
    ).encode()
    assert social.parse_dex(dex, "dex_boosts") == [("dex_boosts", "MintA", 30.0, "")]
    gt = json.dumps(
        {"data": [{"attributes": {"name": "X / SOL"},
                   "relationships": {"base_token": {"data": {"id": "solana_MintB"}}}}]}
    ).encode()  # fmt: skip
    assert social.parse_gt(gt) == [("gt_trending", "MintB", 1.0, "X / SOL")]
    rss = (
        b"<rss><channel><item><title>Orcl Stock</title><ht:approx_traffic>500+"
        b"</ht:approx_traffic></item><item><title>ab</title></item></channel></rss>"
    )
    assert social.parse_google(rss) == [("google_trends", "orclstock", 1.0, "Orcl Stock (500+)")]


def test_state_expires_old_signals_and_keeps_best_ranks():
    st = social.update({}, [("gt_trending", "M", 5.0, ""), ("wiki_top", "pike", 9.0, "")], 0)
    st = social.update(st, [("gt_trending", "M", 2.0, "")], 1_000)
    assert st["mints"]["M"]["gt_trending"] == [2.0, 1_000]
    st = social.update(st, [], social.KEEP_MS + 500)
    assert "keywords" in st and "pike" not in st["keywords"]
    assert "M" in st["mints"]


def test_signals_match_mint_name_and_ticker():
    st = social.update({}, [("dex_boosts", "M", 10.0, ""), ("wiki_top", "pike", 3.0, "")], 0)
    assert social.signals(st, "M", "Pike!", "PK") == {
        "loaded": True, "dex_boosts": 10.0, "wiki_top": 3.0, "any": True
    }  # fmt: skip
    assert social.signals(st, "other", "zzz", "") == {"loaded": True, "any": False}
    assert social.signals(None, "M", "", "") == {"loaded": False}
