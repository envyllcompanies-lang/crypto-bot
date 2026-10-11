"""Labelled outcomes joined from a paper run's shadow labels variant and gates log."""

from cryptobot.labels import build, report
from cryptobot.paper import EVENT_SCHEMAS, OUT_SCHEMAS, Engine, run_replay
from cryptobot.recorder import Sink
from cryptobot.v1 import conviction_variant, labels_universe
from tests.test_conviction import LOOKUP_MS, dump_tape
from tests.test_holders import CLEAN, lookup_row
from tests.test_paper import create_row


def test_labels_join_outcomes_to_features_and_narrative(tmp_path):
    run = tmp_path / "paper" / "run1"
    events = run / "events"
    sink = Sink(events, EVENT_SCHEMAS)
    sink.add("create", [create_row()])
    sink.add("trade", [dict(r, timestamp=r["received_ms"] // 1000) for r in dump_tape().rows])
    verdict = {"narrative_update": True, "narrative": {"score": 4, "red_flags": []}}
    sink.add("lookups", [lookup_row(CLEAN, LOOKUP_MS), lookup_row(verdict, LOOKUP_MS + 1)])
    sink.flush()
    var = conviction_variant(labels_universe(), 0.45, 0.0, shadow=True)
    eng = Engine([var], out=Sink(run, OUT_SCHEMAS))
    run_replay(eng, events)

    df = build((tmp_path / "paper",))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["mint"] == "Mint1111" and row["run"] == "run1"
    assert row["narrative_score"] == 4 and row["x_community"] == 1
    assert row["score"] is not None and row["pts_not_copycat"] == 20
    assert row["win"] == (row["pnl_lamports"] > 0)
    text = "\n".join(report(df))
    assert "- Label trades: 1 from 1 run(s) (paper: 1)" in text
    assert row["exit"] == "tp45|sl0" and row["x_kind"] == "community"
    assert row["holders_differ"] == 1 and row["funding_ok"] == 1
    assert row["narrative_flags"] == 0


def test_no_runs_means_no_labels(tmp_path):
    assert build((tmp_path / "nothing",)) is None


def test_split_table_compares_each_side_of_the_median():
    import polars as pl

    from cryptobot.labels import split_table

    n = 40
    df = pl.DataFrame(
        {
            "dev_share": [i / n for i in range(n)],
            "net_return": [0.4 if i < n // 2 else -0.1 for i in range(n)],
            "pnl_lamports": [1 if i < n // 2 else -1 for i in range(n)],
        }
    ).with_columns(win=pl.col("pnl_lamports") > 0)
    rows = [r for r in split_table(df) if r.startswith("| dev_share")]
    assert rows == [
        "| dev_share (median 0.4875) | 20, 100.0% [83.9%-100.0%], 40.0% | "
        "20, 0.0% [0.0%-16.1%], -10.0% |"
    ]


def test_best_section_counts_profitable_variants():
    import polars as pl

    from cryptobot.m6_report import best_section

    rows = [("good", 0.10 + (i % 2) * 0.01) for i in range(40)]
    rows += [("bad", -0.30 + (i % 2) * 0.62) for i in range(40)]  # mean +1%, wide
    rows += [("tiny", 0.5)] * 5  # too few trades to count
    trades = pl.DataFrame(rows, schema=["variant", "net_return"], orient="row")
    text = "\n".join(best_section(trades))
    assert "- Variants with at least 30 trades: 2" in text
    assert (
        "- With a positive mean: 2" in text and "- With the 95% lower bound above zero: 1" in text
    )
    assert text.index("| good |") < text.index("| bad |") and "tiny" not in text


def test_report_breaks_profit_down_by_filter_and_thesis():
    import polars as pl

    from cryptobot.labels import LIVE_ONLY, report

    n = 40
    base = {
        "exit": ["tp5|sl10"] * n,
        "run": ["r"] * n,
        "source": ["paper"] * n,
        "entry_decided_ts_ms": list(range(n)),
        "age_s": [5.0] * n,
        "copycat": [0] * n,
        "pts_no_bundle_candle": [6] * n,
        "dev_share": [0.01] * n,
        "insider_share": [0.1] * n,
        "largest_holder_share": [0.03] * n,
        "early_buyer_share": [0.1] * n,
        "creator_graduates": [0] * n,
        "creator_recent_launch": [0] * n,
        # Half the tokens have a legit thesis and win +10%; the rest lose 10%.
        "x_kind": ["community" if i % 2 else "tweet" for i in range(n)],
        "narrative_score": [5 if i % 2 else 2 for i in range(n)],
        "narrative_flags": [0] * n,
        "holders_differ": [1] * n,
        "funding_ok": [1] * n,
        "has_telegram": [0] * n,
        "has_website": [1] * n,
        "tracked_holders": [0] * n,
        "net_return": [0.1 if i % 2 else -0.1 for i in range(n)],
        "pnl_lamports": [1 if i % 2 else -1 for i in range(n)],
    }
    df = pl.DataFrame(base).with_columns(win=pl.col("pnl_lamports") > 0)
    df = df.with_columns(**{c: pl.col(c).cast(t) for c, t in LIVE_ONLY.items() if c in df.columns})
    text = "\n".join(report(df))
    assert "| legit thesis | 20 | 100.0%" in text and "| everything else | 20 | 0.0%" in text
    assert "| X link is a community: yes | 20 |" in text
    assert "| 5/5 | 20 |" in text and "| 2/5 | 20 |" in text
    assert "| + top-5 funding not clustered (live) | 40 |" in text
