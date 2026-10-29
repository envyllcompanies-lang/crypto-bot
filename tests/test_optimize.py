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


def test_graduation_check_counts_pool_valued_exits():
    import polars as pl

    from cryptobot import optimize, screen

    df = pl.DataFrame({
        "entry": ["p90"] * 4, "exit": ["hold1m"] * 4, "age": ["5-15m"] * 4,
        "led": [True] * 4, "period": ["holdout"] * 4,
        "flag": ["", "", "graduated_pool", "graduated_pool_open"],
        "net_return": [-0.03, -0.03, 0.60, 0.50],
    })  # fmt: skip
    cell = {"entry": "p90", "exit": "hold1m", "age": "5-15m", "led": "yes"}
    g = optimize.graduation_check(df, cell)
    assert g["share"] == 0.5 and abs(g["rest_mean"] + 0.03) < 1e-12
    assert abs(g["graduated_mean"] - 0.55) < 1e-12
    s = screen._summary(df, ["entry"])
    assert s["grad"].item() == 0.5
    assert "PumpSwap pool" in screen.graduation_note(df)
    assert "final curve price" in screen.graduation_note(df.with_columns(flag=pl.lit("")))
