"""The pre-registered tests are judged exactly as their documents say."""

import json

import polars as pl

from cryptobot import prereg
from cryptobot.paper import OUT_SCHEMAS
from cryptobot.review import load

QUICK = "quick_p90_leader|hold1m|d2s|direct"
CONTROL = "quick_p90|hold1m|d2s|direct"
T0 = prereg.TESTS[2].start_ms + 1000


def trade(variant, i, ret, exit_reason="stall", flags="", t0=T0):
    return {
        "variant": variant,
        "universe": variant.split("|")[0],
        "mint": f"M{i}",
        "entry_decided_ts_ms": t0 + i * 60_000,
        "exit_ts_ms": t0 + i * 60_000 + 61_000,
        "pnl_lamports": int(ret * 25e7),
        "net_return": ret,
        "exit_reason": exit_reason,
        "flags": flags,
    }


def run(root, name, rows, mode="live"):
    d = root / name / "trades" / "2026-10-20"
    d.mkdir(parents=True)
    schema = {k: v for k, v in OUT_SCHEMAS["trades"].items() if k in rows[0]}
    pl.DataFrame(rows, schema=schema).write_parquet(d / "a.parquet")
    (root / name / "summary.json").write_text(json.dumps({"mode": mode}))
    names = {r["variant"] for r in rows}
    (root / name / "variants.json").write_text(
        json.dumps([{"name": n, "shadow": True} for n in names])
    )


def test_stops_at_the_300th_trade_and_passes_on_the_registered_rule(tmp_path):
    rows = [trade(QUICK, i, 0.02 + (0.01 if i % 2 else -0.01)) for i in range(350)]
    rows += [trade(CONTROL, i, -0.01) for i in range(350)]
    run(tmp_path, "r1", rows)
    run(tmp_path, "r1-replay", rows, mode="replay")  # would double every trade
    trades, _ = load(tmp_path)
    test = prereg.TESTS[2]
    cut, why = prereg.stop_time(trades, test)
    assert why.startswith("300 trades") and cut == T0 + 299 * 60_000 + 61_000
    text = "\n".join(prereg.judge(trades, test, now_ms=cut + 1))
    assert "**Finished.**" in text
    assert "| quick_p90_leader\\|hold1m\\|d2s\\|direct | 300 | 2.0% |" in text
    assert "**pass**" in text
    assert "minus quick_p90\\|hold1m\\|d2s\\|direct: 3.0%" in text


def test_too_few_trades_by_the_date_is_not_a_pass(tmp_path):
    run(tmp_path, "r1", [trade(QUICK, i, 0.05) for i in range(120)])
    trades, _ = load(tmp_path)
    test = prereg.TESTS[2]
    text = "\n".join(prereg.judge(trades, test, now_ms=test.end_ms + 1))
    assert "too few trades (120 of 300)" in text and "**pass**" not in text


def test_a_running_test_is_provisional(tmp_path):
    run(tmp_path, "r1", [trade(QUICK, i, 0.05) for i in range(10)])
    trades, _ = load(tmp_path)
    text = "\n".join(prereg.judge(trades, prereg.TESTS[2], now_ms=T0 + 3_600_000))
    assert "Running (provisional, not a verdict)" in text and "so far" in text


def test_graduation_exits_and_the_26_oct_fix_are_split_out(tmp_path):
    rows = [trade(QUICK, i, -0.03) for i in range(9)]
    rows.append(trade(QUICK, 9, 0.60, exit_reason="graduated", flags="graduated"))
    after = prereg.FIX_MS + 1000
    rows += [trade(QUICK, 100 + i, -0.01, t0=after) for i in range(5)]
    run(tmp_path, "r1", rows)
    trades, _ = load(tmp_path)
    text = "\n".join(prereg.judge(trades, prereg.TESTS[2], now_ms=prereg.TESTS[2].end_ms + 1))
    row = next(ln for ln in text.splitlines() if ln.startswith("| quick_p90_leader") and "at" in ln)
    assert "| 6.7% |" in row  # 1 of 15 open at graduation
    assert "| 10 at 3.3% | 5 at -1.0% |" in row  # before and after the 26 Oct fix


def test_elite_stops_when_any_variant_reaches_150_and_reports_the_base_rate(tmp_path):
    test = prereg.TESTS[0]
    t0 = test.start_ms + 1000
    rows = [trade(prereg.ELITE[0], i, 0.01, t0=t0) for i in range(160)]
    rows += [trade(prereg.ELITE[4], i, 0.03, t0=t0) for i in range(40)]
    rows += [trade("labels|tp5|sl10|d2s", i, -0.02, t0=t0) for i in range(200)]
    run(tmp_path, "r1", rows)
    trades, _ = load(tmp_path)
    cut, why = prereg.stop_time(trades, test)
    assert "150 trades" in why
    text = "\n".join(prereg.judge(trades, test, now_ms=cut + 1))
    assert "Base rate over the same days" in text and "-2.0%" in text
    assert "Lower bound for 8 tests" in text


def test_a_pass_carried_by_graduation_exits_is_labelled(tmp_path):
    rows = [trade(QUICK, i, -0.02) for i in range(280)]
    rows += [trade(QUICK, 280 + i, 0.60, exit_reason="graduated") for i in range(20)]
    run(tmp_path, "r1", rows)
    trades, _ = load(tmp_path)
    text = "\n".join(prereg.judge(trades, prereg.TESTS[2], now_ms=prereg.TESTS[2].end_ms + 1))
    assert "**pass**, but only through graduation exits" in text
