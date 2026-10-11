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

    df = build(tmp_path / "paper")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["mint"] == "Mint1111" and row["run"] == "run1"
    assert row["narrative_score"] == 4 and row["x_community"] == 1
    assert row["score"] is not None and row["pts_not_copycat"] == 20
    assert row["win"] == (row["pnl_lamports"] > 0)
    text = "\n".join(report(df))
    assert "- Tokens: 1 from 1 run(s)" in text


def test_no_runs_means_no_labels(tmp_path):
    assert build(tmp_path / "nothing") is None


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
