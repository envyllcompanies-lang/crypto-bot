"""Creator records from recorded creates and trades."""

import polars as pl

from cryptobot.creators import load, records, save
from cryptobot.curve import GRADUATION_VIRTUAL_TOKENS, INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS


def _creates():
    return pl.DataFrame(
        {
            "mint": ["a", "b", "c", "m"],
            "creator": ["x", "x", "y", "x"],
            "slot": [1, 2, 3, 4],
            "received_ms": [1_000, 5_000, 2_000, 9_000],
            "virtual_sol_reserves": [INITIAL_VIRTUAL_SOL] * 4,
            "is_mayhem_mode": [False, False, False, True],
        }
    )


def _trades():
    return pl.DataFrame(
        {
            "mint": ["a", "a", "b", "c", "m"],
            "virtual_token_reserves": [
                INITIAL_VIRTUAL_TOKENS // 2,
                GRADUATION_VIRTUAL_TOKENS,  # a sold out: graduated
                INITIAL_VIRTUAL_TOKENS // 2,
                INITIAL_VIRTUAL_TOKENS,
                GRADUATION_VIRTUAL_TOKENS,  # Mayhem: not counted
            ],
        }
    )


def test_records_count_classic_launches_and_graduates():
    df = records(_creates(), _trades())
    rows = {r["creator"]: r for r in df.iter_rows(named=True)}
    assert rows["x"] == {"creator": "x", "launches": 2, "graduated": 1, "last_launch_ms": 5_000}
    assert rows["y"]["graduated"] == 0


def test_save_and_load(tmp_path):
    path = tmp_path / "creators.parquet"
    save(records(_creates(), _trades().lazy()), {"days": 1}, path)
    assert load(path) == {"x": (2, 1, 5_000), "y": (1, 0, 2_000)}
    assert load(tmp_path / "none.parquet") is None


def test_load_drops_creators_that_cannot_change_a_score(tmp_path):
    from cryptobot.creators import RECENT_LAUNCH_MS

    path = tmp_path / "creators.parquet"
    now = 100 * RECENT_LAUNCH_MS
    df = pl.DataFrame(
        {
            "creator": ["grad", "recent", "stale"],
            "launches": [3, 1, 1],
            "graduated": [1, 0, 0],
            "last_launch_ms": [0, now, now - RECENT_LAUNCH_MS - 1],
        }
    )
    save(df, {}, path)
    assert set(load(path)) == {"grad", "recent"}
