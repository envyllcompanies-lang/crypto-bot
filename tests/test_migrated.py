"""Post-graduation bars and strategies (migrated.py)."""

import numpy as np
import polars as pl

from cryptobot import migrated, pumpswap

SOL, TOK = 85_000_000_000, 206_900_000_000_000
WSOL, MINT = pumpswap.WSOL, "Mint111"


def swaps_file(tmp_path, rows):
    df = pl.DataFrame(
        rows,
        schema={"pool": pl.String, "slot": pl.Int64, "tx_index": pl.Int64,
                "input_mint": pl.String, "input_amount": pl.Int64,
                "output_amount": pl.Int64, "ingested_at": pl.Int64},
        orient="row",
    ).with_columns(pl.col("ingested_at").cast(pl.Datetime("ms")))  # fmt: skip
    path = tmp_path / "swap.parquet"
    df.write_parquet(path)
    return path


def grads():
    return pl.DataFrame({
        "pid": [0], "mint": [MINT], "pool": ["P"], "grad_slot": [100], "grad_ms": [1_000_000],
        "sol_amount": [SOL], "mint_amount": [TOK], "k": [float(SOL) * float(TOK)],
    }).with_columns(pl.col("pid").cast(pl.Int32))  # fmt: skip


def test_bars_match_pumpswap_reserves_and_keep_only_the_window(tmp_path):
    k = float(SOL) * float(TOK)
    f = pumpswap.POOL_FEE_BPS / 10_000
    # a buy of 1 SOL, then a sell of tokens, in minute 0; a buy in minute 2; one before
    # graduation (slot 99) and one after the window, both dropped
    buy = 1_000_000_000
    rows = [
        ("P", 101, 0, WSOL, buy, int(TOK - k / (SOL + buy * (1 - f))), 1_000_500),
        ("P", 102, 0, MINT, 1_000_000_000_000, 4_000_000_000, 1_030_000),
        ("P", 99, 0, WSOL, buy, 1, 999_000),
        ("P", 110, 0, WSOL, buy, 10**12, 1_000_000 + 2 * 60_000 + 5),
        ("P", 999, 0, WSOL, buy, 10**12, 1_000_000 + migrated.WINDOW_MS + 1),
        ("Q", 105, 0, WSOL, buy, 10**12, 1_000_600),  # not a graduate pool
    ]
    path = swaps_file(tmp_path, rows)
    b = migrated.bars(path, grads())
    assert sorted(b["minute"].to_list()) == [0, 2]
    swaps = pl.DataFrame({
        "sol_in": [buy, 0, buy], "tok_out": [rows[0][5], 0, 10**12],
        "tok_in": [0, 1_000_000_000_000, 0], "sol_out": [0, 4_000_000_000, 0],
        "ts": [1_000_500, 1_030_000, 1_120_005],
    })  # fmt: skip
    _, s_ref, _ = pumpswap.pool_path(SOL, TOK, swaps)
    m0 = b.filter(pl.col("minute") == 0).row(0, named=True)
    assert np.isclose(m0["close"], s_ref[1], rtol=1e-6)  # the last swap of minute 0
    assert np.isclose(m0["high"], max(s_ref[:2]), rtol=1e-6)
    assert m0["n"] == 2


def toy(paths: dict[int, list[tuple[int, float]]]):
    """Bars for toy pools: pid -> [(minute, price as a multiple of graduation)]; each
    bar's low and high equal its close unless a tuple of (minute, close, low, high)."""
    rows, g = [], []
    grad_ms = migrated.FIRST_MS + 86_400_000
    for pid, bars in paths.items():
        for bar in bars:
            minute, c, lo, hi = bar if len(bar) == 4 else (*bar, bar[1], bar[1])
            rows.append((pid, minute, *(float(SOL * x**0.5) for x in (c, lo, hi)), 1))
        g.append((pid, f"M{pid}", f"P{pid}", 0, grad_ms, SOL, TOK, float(SOL) * float(TOK)))
    b = pl.DataFrame(rows, schema={"pid": pl.Int32, "minute": pl.Int32, "close": pl.Float64,
                                   "low": pl.Float64, "high": pl.Float64, "n": pl.Int32},
                     orient="row")  # fmt: skip
    grads = pl.DataFrame(g, schema={"pid": pl.Int32, "mint": pl.String, "pool": pl.String,
                                    "grad_slot": pl.Int64, "grad_ms": pl.Int64,
                                    "sol_amount": pl.Int64, "mint_amount": pl.Int64,
                                    "k": pl.Float64}, orient="row")  # fmt: skip
    classic = grads.select("mint")
    return migrated.prepare(b, grads, classic)


END = migrated.END_MINUTE


def test_bottom_bid_buys_the_reversal_a_bar_later_and_sells_at_the_target_or_the_low():
    t = toy({
        # up to 4x, down to 0.8x (-80%), back to 1.1x (+37% off the low): signal at 70
        0: [(0, 1.0), (10, 4.0), (60, 0.8), (70, 1.1), (71, 1.2), (200, 2.0, 2.0, 2.5),
            (201, 2.6), (END, 3.0)],
        # the same setup, then it breaks the low it bought off
        1: [(0, 1.0), (10, 4.0), (60, 0.8), (70, 1.1), (71, 1.2), (90, 0.9, 0.7, 0.9),
            (91, 0.6), (END, 0.5)],
    })  # fmt: skip
    x = migrated.entries(t, drop=0.7, rise=0.3, lo_min=30, hi_min=1440)
    assert x.sort("pid")["sig_m"].to_list() == [70, 70]
    assert x.sort("pid")["in_m"].to_list() == [71, 71]  # filled at the next bar
    r = migrated.bottom_bid(x, target=2.0).sort("pid")
    assert r["reason"].to_list() == ["target", "stop"]
    assert r["out_m"].to_list() == [201, 91]  # sold a bar after the signal, not at it
    win, loss = r["net_return"].to_list()
    cost = (1 - migrated.FEE) ** 2  # the fee both ways; our own impact takes ~0.5% more
    assert 0.99 < (1 + win) / (2.6 / 1.2 * cost) < 1.0
    assert 0.99 < (1 + loss) / (0.6 / 1.2 * cost) < 1.0


def test_no_signal_without_the_fall_or_outside_the_age_window():
    t = toy({0: [(0, 1.0), (10, 2.0), (60, 1.0), (70, 1.5), (END, 1.5)]})  # only -50%
    assert migrated.entries(t, 0.6, 0.3, 30, 1440).is_empty()
    t = toy({0: [(0, 1.0), (10, 4.0), (20, 0.8), (25, 1.2), (END, 1.2)]})  # at 25 min
    assert migrated.entries(t, 0.7, 0.3, 30, 1440).is_empty()


def test_conviction_sells_in_stages_and_the_rest_on_the_trailing_stop():
    t = toy({
        0: [(0, 1.0), (10, 4.0), (60, 0.8), (70, 1.1), (71, 1.0), (100, 2.1), (101, 2.2),
            (200, 4.1), (201, 4.2), (300, 5.0), (400, 1.9), (401, 1.8), (END, 1.0)],
    })  # fmt: skip
    x = migrated.entries(t, 0.7, 0.3, 30, 1440)
    r = migrated.conviction(x, add=False)
    assert r["reason"].item() == "trail"  # 1.9 is more than 60% below 5.0
    # a third at ~2.2x, a third at ~4.2x, the rest at ~1.8x, less fees and impact
    expected = (2.2 + 4.2 + 1.8) / 3 * (1 - migrated.FEE) ** 2
    assert 0.99 < (1 + r["net_return"].item()) / expected < 1.0
    # no fall to half the entry: the averaging-down version is the same trade
    same = migrated.conviction(x, add=True)
    assert same["add_m"].item() is None
    assert same["net_return"].item() == r["net_return"].item()


def test_a_bottom_bid_with_no_exit_is_sold_at_72_hours():
    t = toy({0: [(0, 1.0), (10, 4.0), (60, 0.8), (70, 1.1), (71, 1.2), (END, 1.5)]})
    r = migrated.bottom_bid(migrated.entries(t, 0.7, 0.3, 30, 1440), target=2.0)
    assert r["reason"].item() == "time" and r["out_m"].item() == END
    assert 0.99 < (1 + r["net_return"].item()) / (1.5 / 1.2 * (1 - migrated.FEE) ** 2) < 1.0


def test_averaging_down_buys_a_second_lot_below_half_the_entry():
    t = toy({
        0: [(0, 1.0), (10, 4.0), (60, 0.8), (70, 1.1), (71, 1.0), (100, 0.45), (101, 0.45),
            (300, 2.1), (301, 2.1), (END, 2.1)],
    })  # fmt: skip
    x = migrated.entries(t, 0.7, 0.3, 30, 1440)
    one, two = migrated.conviction(x, add=False), migrated.conviction(x, add=True)
    assert two["add_m"].item() == 100 and two["lot2_m"].item() == 101  # filled a bar later
    # 0.45 is 55% below the top close of 1.0, under the 60% trail: still held. The
    # second lot, bought at 0.45, does far better than the first.
    assert two["net_return"].item() > one["net_return"].item()


def test_base_rate_buys_at_one_hour_and_holds_to_72_hours():
    t = toy({0: [(0, 1.0), (59, 2.0), (61, 2.0), (62, 2.0), (3000, 1.0), (END, 1.0)]})
    r = migrated.base_rate(t)  # bought at minute 62 (a bar after 61), held to the end:
    expected = 0.5 * (1 - migrated.FEE) ** 2  # the last trade's price, 1.0, at 72 hours
    assert 0.99 < (1 + r["net_return"].item()) / expected < 1.0


def test_hours_the_files_miss_are_found_and_their_coins_left_out():
    hour = migrated.HOUR_MS // migrated.MINUTE_MS
    start = migrated.FIRST_MS + 86_400_000
    # each pool trades every minute of its three days; pool 0's hours 30-34 hold one
    # swap an hour (a partial file); pool 1 graduates 40 hours later, after the hole
    rows = []
    for pid, offset in ((0, 0), (1, 40 * hour)):
        for m in range(0, END + 1):
            wall = offset + m
            if pid == 0 and 30 * hour <= wall < 35 * hour and wall % hour:
                continue
            rows.append((pid, m, float(SOL), float(SOL), float(SOL), 5))
    b = pl.DataFrame(rows, schema={"pid": pl.Int32, "minute": pl.Int32, "close": pl.Float64,
                                   "low": pl.Float64, "high": pl.Float64, "n": pl.Int32},
                     orient="row")  # fmt: skip
    grads = pl.DataFrame(
        {"pid": [0, 1], "mint": ["M0", "M1"], "pool": ["P0", "P1"], "grad_slot": [0, 0],
         "grad_ms": [start, start + 40 * migrated.HOUR_MS], "sol_amount": [SOL, SOL],
         "mint_amount": [TOK, TOK], "k": [float(SOL) * TOK] * 2},
        schema_overrides={"pid": pl.Int32},
    )  # fmt: skip
    first = start // migrated.HOUR_MS
    assert migrated.gaps(b, grads) == list(range(first + 30, first + 35))
    assert migrated.gap_spans(migrated.gaps(b, grads))[0][1].endswith("11:00")
    t = migrated.prepare(b, grads, grads.select("mint"), migrated.gaps(b, grads))
    assert t["pid"].unique().to_list() == [1]
