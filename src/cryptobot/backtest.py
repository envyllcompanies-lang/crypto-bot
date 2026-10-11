"""M3 backtest: the paper engine replayed over Slinky21's historical trades.

The same engine, variants and fill model as the live paper trader (paper.py), fed by
slinky.events instead of the websocket, so backtest and live results compare directly.
Differences from live, all because the history has no lookups:

- V1's holder, funding and X-link checks are not evaluated (skipped, as live when a
  lookup fails). The tracked-wallet check is skipped too: the nightly list was built
  from later data, so using it would leak the future.
- Conviction runs offline (conviction.OFFLINE): no funding block, half points for the
  holder check, X link not evaluated, no creator file (only launches seen in the replay).
- The narrative-gated universe never trades: there are no verdicts.

The run folder has the paper trader's layout (data/backtest/<name>/), so
`m6_report <folder>` writes its report and `labels.py` reads its labelled outcomes.

Usage: uv run python -m cryptobot.backtest [--from 2026-09-25] [--to 2026-10-09]
On the server, deploy/backtest.sh runs it in the background with a memory cap.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from cryptobot.curve import to_lamports, to_sol
from cryptobot.paper import OUT_SCHEMAS, PAPER, Engine
from cryptobot.recorder import Sink
from cryptobot.slinky import SLINKY_ROOT, events
from cryptobot.v1 import default_variants

BACKTEST = PAPER.parent / "backtest"
PROGRESS_EVERY_MS = 3_600_000


def _day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def run(
    out: Path,
    start: datetime | None,
    end: datetime | None,
    root: Path = SLINKY_ROOT,
    max_stake_sol: float = 1.0,
) -> Engine:
    variants = default_variants(to_lamports(max_stake_sol))
    out.mkdir(parents=True, exist_ok=True)
    (out / "variants.json").write_text(json.dumps([v.describe() for v in variants], indent=1))
    engine = Engine(variants, out=Sink(out, OUT_SCHEMAS))
    engine.holder_checks = False
    engine.offline = True
    engine.keep_results = False
    engine.summary_path = out / "summary.json"
    engine.summary_extra = {
        "mode": "backtest",
        "source": str(root),
        "running": True,
        "from": None if start is None else start.isoformat(),
        "to": None if end is None else end.isoformat(),
    }
    began, next_note = time.time(), None
    for kind, row in events(root, start, end):
        if kind == "create":
            engine.on_create(row)
        else:
            engine.on_trade(row)
        ts = row["received_ms"]
        if next_note is None:
            next_note = ts + PROGRESS_EVERY_MS
        elif ts >= next_note:
            at = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M")
            print(
                f"{at} UTC: {engine.closed_trades:,} closed trades, "
                f"{time.time() - began:,.0f} s elapsed",
                flush=True,
            )
            next_note = ts + PROGRESS_EVERY_MS
    engine.finish()
    engine.summary_extra["running"] = False
    engine.write_summary()
    return engine


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", type=_day, help="first UTC day (default: all)")
    parser.add_argument("--to", dest="end", type=_day, help="stop before this UTC day")
    parser.add_argument("--out", type=Path, help="run folder (default data/backtest/<now>)")
    parser.add_argument("--max-stake-sol", type=float, default=1.0)
    args = parser.parse_args(argv)
    out = args.out or BACKTEST / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    engine = run(out, args.start, args.end, max_stake_sol=args.max_stake_sol)
    pnl = to_sol(engine.closed_pnl_lamports)
    print(f"{out}: {engine.closed_trades:,} closed trades, {pnl:+.4f} SOL across all variants")
    print(f"report: uv run python -m cryptobot.m6_report {out}")


if __name__ == "__main__":
    main()
