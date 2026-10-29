"""Tracked-wallet scoring, the V1 check that uses it, and recording it for replays."""

import os

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_SOL, LAMPORTS_PER_SOL
from cryptobot.paper import EVENT_SCHEMAS, Engine, TrackedFeed, run_replay
from cryptobot.recorder import Sink
from cryptobot.tracked import holdout, load_list, positions, save, score
from cryptobot.v1 import NEW_PAIRS_STAKE, Universe, check
from tests.test_paper import create_row
from tests.test_v1_features import Tape, token_from

SOL = LAMPORTS_PER_SOL
HOUR = 3_600_000


def _creates(n: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "mint": [f"T{i}" for i in range(n)],
            "creator": ["devA"] + [f"dev{i}" for i in range(1, n)],
            "slot": [100] * n,
            "virtual_sol_reserves": [INITIAL_VIRTUAL_SOL] * n,
            "is_mayhem_mode": [False] * n,
        }
    )


def _round_trip(user, mint, t0, buy_slot, cost, back, hold_ms=60_000):
    """One wallet buying 1,000 tokens and selling them all later."""
    common = {"user": user, "mint": mint, "token_amount": 1_000, "fee": 0, "creator_fee": 0}
    return [
        {**common, "is_buy": True, "sol_amount": cost, "slot": buy_slot, "ts_ms": t0},
        {
            **common,
            "is_buy": False,
            "sol_amount": back,
            "slot": buy_slot + 50,
            "ts_ms": t0 + hold_ms,
        },
    ]


def _market(n_tokens: int = 8) -> pl.DataFrame:
    rows = []
    for i in range(n_tokens):
        mint, t0 = f"T{i}", i * HOUR
        rows += _round_trip("pro", mint, t0 + 10_000, 110, SOL, int(1.5 * SOL))
        rows += _round_trip("sniper", mint, t0, 100, SOL, int(1.5 * SOL))
        rows += _round_trip("devA", mint, t0 + 20_000, 110, SOL, int(1.5 * SOL))
        rows += _round_trip("flipper", mint, t0 + 30_000, 110, SOL, int(1.2 * SOL), hold_ms=1_000)
        rows += _round_trip("loser", mint, t0 + 40_000, 110, SOL, int(0.6 * SOL))
    return pl.DataFrame(rows)


def test_score_keeps_skilled_wallets_and_drops_bots():
    trades, creates = _market(), _creates(8)
    scored = score(positions(trades, creates), creates)
    by = {r["user"]: r for r in scored.iter_rows(named=True)}
    assert by["pro"]["tracked"] and not by["pro"]["bot"]
    assert by["pro"]["closed"] == 8 and by["pro"]["pnl"] == 4 * SOL
    assert by["sniper"]["bot"] and not by["sniper"]["tracked"]  # buys in the launch slot
    assert by["devA"]["bot"]  # created a token
    assert by["flipper"]["bot"]  # holds for a second
    assert not by["loser"]["tracked"] and not by["loser"]["bot"]


def test_fees_count_against_the_wallet():
    trades = _market().with_columns(
        fee=pl.when(pl.col("user") == "pro").then(int(0.3 * SOL)).otherwise(0)
    )
    creates = _creates(8)
    by = {r["user"]: r for r in score(positions(trades, creates), creates).iter_rows(named=True)}
    # 0.5 SOL gross per trip, minus 0.3 SOL fee on each side: every trip now loses.
    assert by["pro"]["wins"] == 0 and not by["pro"]["tracked"]


def test_partly_sold_positions_are_not_closed():
    trades = _market().with_columns(
        token_amount=pl.when((pl.col("user") == "pro") & ~pl.col("is_buy"))
        .then(500)
        .otherwise(pl.col("token_amount"))
    )
    creates = _creates(8)
    pro = (
        score(positions(trades, creates), creates)
        .filter(pl.col("user") == "pro")
        .row(0, named=True)
    )
    assert pro["closed"] == 0 and not pro["tracked"]


def test_mayhem_and_non_sol_launches_are_ignored():
    creates = _creates(8).with_columns(is_mayhem_mode=pl.lit(True))
    assert positions(_market(), creates).height == 0


def test_holdout_tests_the_list_on_later_trades():
    result = holdout(_market(16), _creates(16))
    assert result["usable"] and result["listed_in_train"] == 1
    assert result["listed"]["win_rate"] == 1.0
    assert result["others"]["win_rate"] == 0.0  # only the loser is left once bots go


def test_save_and_load_list(tmp_path):
    trades, creates = _market(), _creates(8)
    path = tmp_path / "tracked.parquet"
    save(score(positions(trades, creates), creates), {"x": 1}, path)
    assert load_list(path) == frozenset({"pro"})
    assert load_list(tmp_path / "missing.parquet") is None


def test_v1_check_counts_tracked_holders():
    u = Universe("new_pairs", "new_pairs", NEW_PAIRS_STAKE)
    t = Tape()
    for user in ("a", "b", "c", "d"):
        t.buy(0, 0.5, user=user)
    tok = token_from(t)
    assert check(u, tok, 1_000)["few_tracked_wallets"] is None  # no list loaded
    assert check(u, tok, 1_000, frozenset({"a", "b"}))["few_tracked_wallets"] is True
    assert check(u, tok, 1_000, frozenset({"a", "b", "c"}))["few_tracked_wallets"] is False
    t.sell(10, "c")
    tok = token_from(t)
    assert check(u, tok, 1_000, frozenset({"a", "b", "c"}))["few_tracked_wallets"] is True


def test_replay_applies_recorded_lists(tmp_path):
    events = Sink(tmp_path, EVENT_SCHEMAS)
    events.add("create", [create_row()])
    events.add(
        "tracked",
        [{"seq": 1, "received_ms": 5, "event_index": 0, "list_mtime_ms": 1, "wallets": ["w"]}],
    )
    events.flush()
    engine = Engine([], unlimited_balance=True)
    run_replay(engine, tmp_path)
    assert engine.tracked == frozenset({"w"}) and engine.stats["tracked_lists"] == 1


def test_live_feed_loads_changed_lists_and_records_them(tmp_path):
    path = tmp_path / "tracked.parquet"
    engine = Engine([], unlimited_balance=True)
    sink = Sink(tmp_path / "events", EVENT_SCHEMAS)
    feed = TrackedFeed(engine, sink, path)
    feed.poll(0)
    assert engine.tracked is None  # no list yet
    pl.DataFrame({"user": ["a", "b"]}).write_parquet(path)
    feed.poll(30_000)
    assert engine.tracked is None  # polls at most once a minute
    feed.poll(60_000)
    assert engine.tracked == frozenset({"a", "b"})
    pl.DataFrame({"user": ["c"]}).write_parquet(path)
    os.utime(path, (10_000, 10_000))
    feed.poll(120_000)
    assert engine.tracked == frozenset({"c"}) and engine.stats["tracked_lists"] == 2
    sink.flush()
    recorded = pl.read_parquet(sorted((tmp_path / "events" / "tracked").rglob("*.parquet")))
    assert recorded["wallets"].to_list() == [["a", "b"], ["c"]]


def test_load_recorded_reads_recent_recorder_files(tmp_path):
    from cryptobot.tracked import load_recorded

    t = Tape()
    t.buy(1_000, 0.5, user="a")
    t.sell(70_000, "a")
    rows = [{**r, "signature": f"s{i}", "fee": 0, "creator_fee": 0} for i, r in enumerate(t.rows)]
    sink = Sink(tmp_path, EVENT_SCHEMAS)
    sink.add("create", [create_row()])
    sink.add("trade", rows + rows[:1])  # a duplicate row is dropped
    sink.flush()
    trades, creates = load_recorded([tmp_path])
    df = trades.collect()
    assert df.height == 2 and set(df["user"]) == {"a"}
    assert creates["mint"].to_list() == [create_row()["mint"]]


def test_recent_days_are_the_last_days_of_data_across_all_runs(tmp_path):
    from cryptobot.tracked import _recent

    def folder(root, day, n=1):
        d = tmp_path / root / "trade" / day
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"{i}.parquet").write_bytes(b"")
        return d

    folder("old_run", "2026-10-01")  # a run that ended weeks ago: not recent
    folder("run", "2026-10-24")
    folder("run", "2026-10-25")  # yesterday: the whole day is inside "1 day"
    folder("run", "2026-10-26")  # today, ten minutes in
    roots = [tmp_path / "old_run", tmp_path / "run"]
    got = {p.parent.name for p in _recent(roots, "trade", 1)}
    assert got == {"2026-10-25", "2026-10-26"}
