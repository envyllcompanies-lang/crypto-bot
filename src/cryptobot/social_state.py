"""The social collector's state and what it says about one coin: shared by social.py
(which writes the state) and the paper trader (which stamps each gate record with
`signals()`). Kept apart from social.py so the paper trader doesn't load, and isn't
restarted for, the collectors themselves (cryptobot.affected)."""

from __future__ import annotations

from cryptobot.recorder import LIVE
from cryptobot.text import MIN_KEY_CHARS, norm

NOW_PATH = LIVE.parent / "derived" / "social_now.json"
KEEP_MS = 6 * 3600 * 1000  # a signal counts for six hours after it was last seen
STALE_MS = 30 * 60 * 1000  # no collector update for 30 minutes: the state is stale

X_KINDS = (  # xscrape.py's per-coin numbers
    "x_ca_posts",
    "x_ca_posts_1h",
    "x_ca_authors",
    "x_ca_likes",
    "x_ca_views",
    "x_ca_max_followers",
    "x_ca_new_share",
    "x_tk_posts_1h",
    "x_tk_authors",
    "x_comm_members",
    "x_comm_mods",
    "x_post_likes",
    "x_post_reposts",
    "x_post_views",
    "x_post_author_followers",
    "x_post_author_days",
    "x_post_age_h",
    "x_prof_followers",
    "x_prof_days",
    "x_prof_posts",
    "x_prof_blue",
)

MINT_SOURCES = (  # kinds whose key is a mint address (the rest are keywords)
    "dex_boosts",
    "dex_profiles",
    "dex_takeovers",
    "gt_trending",
    "biz_mint",
    "bsky_mint",
    "reddit_mint",
    "mastodon_mint",
    "farcaster_mint",
    "tg_mint",
    "tgweb_mint",
    "tg_members",
    "tg_posts_1h",
    "dex_socials",
    "dex_websites",
    "dex_boosts_active",
    "lemmy_mint",
    "x_posts_1h",
    "x_posts_24h",
    "gdelt_vol_24h",
    "gdelt_vol_last",
    "yt_videos_24h",
    "pf_live",
    "pf_streaming",
    "pf_flagged",
    *X_KINDS,
)
# Kinds whose value is a rank (1 = top): lower is better when sources disagree.
RANK_KINDS = {"gt_trending", "google_trends", "wiki_top", "cg_trending", "mastodon_tag",
              "imgflip_meme"}  # fmt: skip
OWN_ACCOUNT_KINDS = {  # describe the coin's own accounts and listings, not mentions of it
    "tg_members",
    "tg_posts_1h",
    "dex_socials",
    "dex_websites",
    *(k for k in X_KINDS if k.startswith(("x_comm_", "x_post_", "x_prof_"))),
    # reach and freshness of those who posted: positive only when there were posts
    "x_ca_likes",
    "x_ca_views",
    "x_ca_max_followers",
    "x_ca_new_share",
}


def update(state: dict, rows: list[tuple[str, str, float, str]], now_ms: int) -> dict:
    """Fold a snapshot into the state: mints and keywords -> {kind: [value, last seen]}."""
    for kind, key, value, _ in rows:
        bucket = "mints" if kind in MINT_SOURCES else "keywords"
        entry = state.setdefault(bucket, {}).setdefault(key, {})
        old = entry.get(kind)
        if kind in ("dex_boosts", "pf_live"):
            # totalAmount is cumulative; for a live stream, keep its peak audience
            value = max(value, old[0]) if old else value
        elif kind in RANK_KINDS and old:
            value = min(value, old[0])  # best rank seen
        entry[kind] = [value, now_ms]
    cut = now_ms - KEEP_MS
    for bucket in ("mints", "keywords"):
        for key in list(state.get(bucket, {})):
            kinds = {k: v for k, v in state[bucket][key].items() if v[1] >= cut}
            if kinds:
                state[bucket][key] = kinds
            else:
                del state[bucket][key]
    state["updated_ms"] = now_ms
    return state


def signals(
    state: dict | None,
    mint: str,
    name: str | None,
    symbol: str | None,
    now_ms: int | None = None,
) -> dict:
    """What the current state says about one coin (the paper trader logs this).

    With `now_ms` (the paper trader passes the event time), a state the collector last
    updated more than STALE_MS earlier counts as not loaded (the collector has stopped:
    its old trending words are not news), and entries older than KEEP_MS are left out."""
    if not state:
        return {"loaded": False}
    if now_ms is not None and state.get("updated_ms", 0) < now_ms - STALE_MS:
        return {"loaded": False, "stale": True}
    cut = now_ms - KEEP_MS if now_ms is not None else None
    out: dict = {"loaded": True}
    for kind, (value, seen) in state.get("mints", {}).get(mint, {}).items():
        if cut is None or seen >= cut:
            out[kind] = value
    for k in {norm(name), norm(symbol)}:
        if len(k) >= MIN_KEY_CHARS:
            for kind, (value, seen) in state.get("keywords", {}).get(k, {}).items():
                if cut is not None and seen < cut:
                    continue
                # name and ticker can both match: the better rank, or the higher count
                best = min if kind in RANK_KINDS else max
                out[kind] = best(value, out.get(kind, value))
    # How many separate sources mention the coin (dex, reddit, x...): "talked about in
    # several places" can then be tested on its own. A per-coin lookup that found nothing
    # (0 posts, 0 videos) is not a mention, and neither are facts about the coin's own
    # accounts (its Telegram's size, its X profile's followers).
    families = {
        kind.split("_")[0]
        for kind, value in out.items()
        if kind != "loaded" and kind not in OWN_ACCOUNT_KINDS and value > 0
    }
    out["any"] = bool(families)
    out["sources"] = len(families)
    return out
