"""The search picks on discovery and reports that pick's holdout result."""

import polars as pl

from cryptobot.optimize import search


def test_search_picks_on_discovery_and_judges_on_holdout():
    rows = []
    # cell A: good on discovery, bad on holdout; cell B: the reverse
    for entry, d, h in (("pA", 0.05, -0.05), ("pB", -0.02, 0.04)):
        for k in range(10):
            for period, m in (("discovery", d), ("holdout", h)):
                rows.append(
                    {"entry": entry, "exit": "hold5m", "age": "0-5m", "led": k % 2 == 0,
                     "period": period, "net_return": m + (k - 4.5) * 1e-3, "flag": ""}
                )  # fmt: skip
    honest, hindsight = search(pl.DataFrame(rows), min_trades=5)
    assert honest.row(0, named=True)["entry"] == "pA"
    assert honest.row(0, named=True)["mean_h"] < 0
    assert hindsight.row(0, named=True)["entry"] == "pB"
    assert set(honest["led"].to_list()) == {"yes", "no", "either"}
