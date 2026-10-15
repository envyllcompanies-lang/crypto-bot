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
        "loaded": True, "dex_boosts": 10.0, "wiki_top": 3.0, "any": True, "sources": 2
    }  # fmt: skip
    assert social.signals(st, "other", "zzz", "") == {"loaded": True, "any": False, "sources": 0}
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


def test_per_coin_results_are_matched_by_mint():
    rows = [("x_posts_24h", "MintX", 12.0, ""), ("gdelt_vol_24h", "MintX", 0.4, ""),
            ("yt_videos_24h", "MintX", 3.0, ""), ("reddit_mint", "MintX", 2.0, ""),
            ("tg_mint", "MintX", 1.0, "")]  # fmt: skip
    st = social.update({}, rows, 0)
    got = social.signals(st, "MintX", "zz", "zz")
    assert got["x_posts_24h"] == 12.0 and got["gdelt_vol_24h"] == 0.4
    assert got["yt_videos_24h"] == 3.0 and got["reddit_mint"] == 2.0 and got["tg_mint"] == 1.0


def test_free_source_parsers():
    import json as j

    from cryptobot import freesocial

    tags = j.dumps([{"name": "FrustratedCartoons"}, {"name": "ab"}]).encode()
    assert freesocial.mastodon_tags(tags) == [
        ("mastodon_tag", "frustratedcartoons", 1.0, "FrustratedCartoons")
    ]
    assert freesocial.mastodon_texts(j.dumps([{"content": "<p>$BONK up</p>"}]).encode()) == [
        " $BONK up "
    ]
    assert freesocial.gdelt_query("Christa Pike!") == '"Christa Pike"'
    assert freesocial.gdelt_query("Dog") is None
    vol = j.dumps({"timeline": [{"data": [{"value": 0.1}, {"value": 0.3}]}]}).encode()
    assert freesocial.gdelt_volume(vol) == {"gdelt_vol_24h": 0.4, "gdelt_vol_last": 0.3}
    assert freesocial.gdelt_volume(b"Please limit requests") is None


def test_stream_counter_prefixes():
    c = social.StreamCounter("tg")
    c.add("aped $BONK", 0)
    assert c.rows(1) == [("tg_ticker", "bonk", 1.0, "")]


def test_imgflip_and_hn_parsers():
    raw = json.dumps({"data": {"memes": [{"name": "Drake Hotline Bling"}]}}).encode()
    assert social.parse_imgflip(raw) == [
        ("imgflip_meme", "drakehotlinebling", 1.0, "Drake Hotline Bling")
    ]
    assert social.hn_titles(json.dumps({"hits": [{"title": "A"}]}).encode()) == ["A"]


def test_reddit_rss_and_lemmy_parsing(monkeypatch):
    from cryptobot import freesocial, reddit

    feed = (
        b'<feed><entry><title>$BONK &amp; friends</title><content type="html">'
        b"&lt;p&gt;buy 9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin&lt;/p&gt;</content>"
        b"</entry></feed>"
    )
    texts = reddit.rss_texts(feed)
    assert texts[0].startswith("$BONK & friends")
    assert "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin" in texts[0]
    payload = {"posts": [{"post": {"name": "$PEPE", "body": None}}]}
    monkeypatch.setattr(freesocial, "_get", lambda url: json.dumps(payload).encode())
    assert freesocial.lemmy_texts("cryptocurrency") == ["$PEPE "]


def test_tgweb_parses_preview_pages_and_counts_only_new_posts(monkeypatch):
    from cryptobot import tgweb

    page = (
        '<div class="tgme_page_extra">12 345 subscribers</div>'
        '<div class="tgme_widget_message_wrap"><div data-post="c/1">'
        '<div class="tgme_widget_message_text js-message_text">old $BONK</div>'
        '<time datetime="2026-10-15T10:00:00+00:00"></time></div>'
        '<div class="tgme_widget_message_wrap"><div data-post="c/2">'
        '<div class="tgme_widget_message_text">new &amp; $PEPE</div>'
        '<time datetime="2026-10-15T11:00:00+00:00"></time></div>'
    )
    assert tgweb.members(page) == 12345
    assert [(p[0], p[1]) for p in tgweb.posts(page)] == [
        ("c/1", "old $BONK"),
        ("c/2", "new & $PEPE"),
    ]
    assert tgweb.handle_of("https://t.me/coin_portal") == "coin_portal"
    assert tgweb.handle_of("https://t.me/+invite") is None
    assert tgweb.handle_of("https://t.me/somebot") is None
    w = tgweb.TgWeb()
    pages = iter([page, page + page.replace("c/2", "c/3")])
    monkeypatch.setattr(w, "_get", lambda url: next(pages))
    assert w.new_posts("c") == []  # first visit only sets the baseline
    assert w.new_posts("c") == ["new & $PEPE"]  # c/3 is new


def test_dex_token_counts_socials(monkeypatch):
    from cryptobot import freesocial

    pair = {"info": {"socials": [{"type": "twitter"}, {"type": "telegram"}], "websites": []},
            "boosts": {"active": 2}}  # fmt: skip
    monkeypatch.setattr(freesocial, "_get", lambda url: json.dumps([pair]).encode())
    assert freesocial.dex_token("M") == {
        "dex_socials": 2.0, "dex_websites": 0.0, "dex_boosts_active": 2.0
    }  # fmt: skip
    monkeypatch.setattr(freesocial, "_get", lambda url: b"[]")
    assert freesocial.dex_token("M") is None
