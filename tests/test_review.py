"""The review pools every run's closed trades."""

import json

import polars as pl

from cryptobot.paper import OUT_SCHEMAS
from cryptobot.review import load, report


def _trades(n, variant, universe, ret, t0=0):
    return [
        {
            "variant": variant,
            "universe": universe,
            "mint": f"M{i}",
            "entry_decided_ts_ms": t0 + i,
            "pnl_lamports": int(ret * 1e9),
            "net_return": ret,
            "exit_reason": "target" if ret > 0 else "stop",
        }
        for i in range(n)
    ]


def _run(root, name, rows, hours, mode="live", shadow=()):
    run = root / name
    (run / "trades" / "2026-10-14").mkdir(parents=True)
    schema = {k: v for k, v in OUT_SCHEMAS["trades"].items() if k in rows[0]}
    pl.DataFrame(rows, schema=schema).write_parquet(run / "trades" / "2026-10-14" / "a.parquet")
    summary = {"first_event_ms": 0, "last_event_ms": int(hours * 3_600_000), "mode": mode}
    (run / "summary.json").write_text(json.dumps(summary | {"closed_trades": len(rows)}))
    variants = [{"name": v, "shadow": v in shadow} for v in {r["variant"] for r in rows}]
    (run / "variants.json").write_text(json.dumps(variants))


def test_review_pools_runs_and_separates_labels(tmp_path):
    _run(tmp_path, "r1", _trades(20, "conviction|tp45|sl0|d2s", "conviction", 0.1), 2.0)
    _run(tmp_path, "r2", _trades(20, "conviction|tp45|sl0|d2s", "conviction", -0.2, 10**6), 3.0)
    _run(tmp_path, "r3", _trades(5, "labels|tp45|sl0|d2s", "labels", 0.3), 1.0)
    trades, runs = load(tmp_path)
    text = "\n".join(report(trades, runs))
    assert "- Runs: 3, covering 6.0 hours of live data" in text
    assert "40 by wallet variants, 5 by shadow variants" in text
    assert "| conviction | 40 | 50.0%" in text  # both runs pooled
    assert "- 1 variants; 0 with a positive mean" in text
    assert "| labels\\|tp45\\|sl0\\|d2s | 5 | 100.0%" in text


def test_review_with_no_runs(tmp_path):
    trades, runs = load(tmp_path / "none")
    assert "No closed trades yet." in "\n".join(report(trades, runs))


def test_replays_are_left_out_and_every_shadow_variant_is_marked(tmp_path):
    late = "late_p90|tp25|sl15|d2s|direct"
    _run(tmp_path, "r1", _trades(30, late, "late_p90", 0.1), 2.0, shadow={late})
    _run(tmp_path, "r1-replay", _trades(30, late, "late_p90", 0.1), 2.0, mode="replay")
    trades, runs = load(tmp_path)
    assert trades.height == 30 and len(runs) == 1  # the replay would double every trade
    assert trades["shadow"].all()  # shadow per variants.json, not only labels|...
    one = pl.concat([trades.head(1)])
    assert "[n/a]" in "\n".join(report(one, runs))  # no interval from one trade
