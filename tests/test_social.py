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


def test_biz_counts_mints_tickers_and_words_across_4chan_word_breaks():
    mint = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    catalog = json.dumps(
        [
            {
                "threads": [
                    {"sub": "pepe", "com": f"{mint[:20]}<wbr>{mint[20:]} buy $FOOD",
                     "last_replies": [{"com": "foodpepe &amp; $FOOD"}]},
                    {"com": "foodpepe again"},
                ]
            }
        ]
    ).encode()  # fmt: skip
    rows = {(k, key): v for k, key, v, _ in social.parse_biz(catalog)}
    assert rows[("biz_mint", mint)] == 1
    assert rows[("biz_ticker", "food")] == 2
    assert rows[("biz_word", "foodpepe")] == 2


def test_spikes_flag_only_words_far_above_their_baseline():
    sp = social.Spikes("biz_spike", alpha=0.5, warmup=2)
    for _ in range(3):
        assert sp.update({"because": 40.0}) == []
    rows = sp.update({"because": 41.0, "foodpepe": 6.0})
    assert [r[1] for r in rows] == ["foodpepe"]


def test_bluesky_counter_keeps_the_last_hour():
    c = social.BlueskyCounter()
    c.add("$PEPE to the moon", 0)
    c.add("$pepe again", social.BSKY_WINDOW_MS)
    assert c.rows(social.BSKY_WINDOW_MS + 1) == [("bsky_ticker", "pepe", 1.0, "")]


def test_coingecko_trending_names_and_tickers_become_keywords():
    raw = json.dumps({"coins": [{"item": {"name": "Astronaut", "symbol": "ASTRO"}}]}).encode()
    assert sorted(social.parse_cg(raw)) == [
        ("cg_trending", "astro", 1.0, "Astronaut"),
        ("cg_trending", "astronaut", 1.0, "Astronaut"),
    ]


def test_count_texts_and_reddit_listing():
    from cryptobot import reddit

    listing = {"data": {"children": [{"data": {"title": "$BONK runs", "selftext": "bonk"}}]}}
    texts = reddit.texts(listing)
    rows, words = social.count_texts(texts, "reddit")
    assert rows == [("reddit_ticker", "bonk", 1.0, "")]
    assert words["bonk"] == 1 and words["runs"] == 1


def test_watchlist_reads_only_new_lines_once(tmp_path):
    path = tmp_path / "watch.jsonl"
    path.write_text('{"mint": "old"}\n')
    w = social.Watchlist(path)
    with path.open("a") as f:
        f.write('{"mint": "A", "name": "x"}\n{"mint": "A"}\nnot json\n')
    assert [i["mint"] for i in w.new()] == ["A"]
    assert w.new() == []
    path.write_text('{"mint": "B"}\n')  # rotated by daily.sh
    assert [i["mint"] for i in w.new()] == ["B"]


def test_x_query_budget_and_summary(tmp_path, monkeypatch):
    from cryptobot import xapi

    assert xapi.query("MINT", "Christa Pike", "$PIKE") == 'MINT OR $PIKE OR "Christa Pike"'
    assert xapi.query("MINT", "ab", "?!") == "MINT"
    b = xapi.Budget(tmp_path / "b.json", monthly_usd=0.01, cost_usd=0.005)
    assert b.take() and b.take() and not b.take()
    assert b.spent() == 0.01
    got = {"data": [{"tweet_count": 3}, {"tweet_count": 7}], "meta": {"total_tweet_count": 10}}
    assert xapi.summarize(got) == {"x_posts_1h": 7.0, "x_posts_24h": 10.0}
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    assert not xapi.configured()
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.setenv("X_MONTHLY_BUDGET_USD", "0")
    assert not xapi.configured()  # no budget, no spending
