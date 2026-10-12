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

Usage: uv run python -m cryptobot.backtest [--from 2026-09-25] [--to 2026-10-09] [--jobs N]
On the server, deploy/backtest.sh runs it in the background with a memory cap.

--jobs N splits the period into N equal parts run side by side (one process each, about
400 MB apiece) and merges them into one run folder. Each part starts with fresh wallets
and empty state, so a token alive across a boundary is seen by the later part only from
its next launch, and wallet balances add up each part's profit and loss rather than
compounding. Per-trade results are unaffected apart from those edges.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptobot.curve import to_lamports, to_sol
from cryptobot.paper import OUT_SCHEMAS, PAPER, START_BALANCE, Engine
from cryptobot.recorder import Sink
from cryptobot.slinky import SLINKY_ROOT, events, files
from cryptobot.v1 import (
    default_variants,
    new_pairs_variants,
    scalp_variants,
    stretch_variants,
    trail_variants,
)

GRIDS = {
    "default": default_variants,
    "scalp": scalp_variants,
    "trail": trail_variants,
    "stretch": stretch_variants,
    "newpairs": new_pairs_variants,
}

BACKTEST = PAPER.parent / "backtest"
PROGRESS_EVERY_MS = 3_600_000
FLUSH_EVERY_MS = 30 * 60 * 1000  # backtests write every 30 min of data, not every minute
TABLES = ("trades", "decisions", "gates")


def _day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def run(
    out: Path,
    start: datetime | None,
    end: datetime | None,
    root: Path = SLINKY_ROOT,
    max_stake_sol: float = 1.0,
    grid: str = "default",
) -> Engine:
    variants = GRIDS[grid](to_lamports(max_stake_sol))
    out.mkdir(parents=True, exist_ok=True)
    (out / "variants.json").write_text(json.dumps([v.describe() for v in variants], indent=1))
    engine = Engine(variants, out=Sink(out, OUT_SCHEMAS))
    engine.holder_checks = False
    engine.offline = True
    engine.keep_results = False
    engine.flush_every_ms = FLUSH_EVERY_MS
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


def data_span(root: Path = SLINKY_ROOT) -> tuple[datetime, datetime]:
    """From 12 hours before the first file's stamp to just after the last one's."""
    chunks = files(root)
    if not chunks:
        raise SystemExit(f"no Slinky21 files under {root}")
    return chunks[0][0] - timedelta(hours=12), chunks[-1][0] + timedelta(minutes=1)


def _part(args: tuple[Path, datetime, datetime, Path, float, str]) -> dict:
    out, start, end, root, stake, grid = args
    engine = run(out, start, end, root, stake, grid)
    return {"closed_trades": engine.closed_trades, "pnl": engine.closed_pnl_lamports}


def run_parallel(
    out: Path,
    start: datetime,
    end: datetime,
    jobs: int,
    root: Path = SLINKY_ROOT,
    stake: float = 1.0,
    grid: str = "default",
) -> dict:
    """Run `jobs` equal parts side by side, then merge them into `out`."""
    step = (end - start) / jobs
    bounds = [(start + step * k, start + step * (k + 1)) for k in range(jobs)]
    parts = [out / "parts" / f"part{k}" for k in range(jobs)]
    # Spawn, not fork: forking a process that already runs Polars threads can deadlock.
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs_args = [(p, s, e, root, stake, grid) for p, (s, e) in zip(parts, bounds, strict=True)]
        list(pool.map(_part, jobs_args))
    return merge(out, parts, start, end)


def merge(out: Path, parts: list[Path], start: datetime, end: datetime) -> dict:
    """One run folder from the parts: their tables side by side, their summaries added."""
    for k, part in enumerate(parts):
        for table in TABLES:
            for f in sorted((part / table).glob("*/*.parquet")):
                dest = out / table / f.parent.name / f"p{k}-{f.name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(f, dest)
    shutil.copy(parts[0] / "variants.json", out / "variants.json")
    summaries = [json.loads((p / "summary.json").read_text()) for p in parts]
    first = summaries[0]
    merged = {
        **first,
        "running": False,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "parts": len(parts),
        "first_event_ms": min(
            (s["first_event_ms"] for s in summaries if s["first_event_ms"]), default=None
        ),
        "last_event_ms": max(
            (s["last_event_ms"] for s in summaries if s["last_event_ms"]), default=None
        ),
        "closed_trades": sum(s["closed_trades"] for s in summaries),
        "stats": {},
    }
    for s in summaries:
        for k, v in s["stats"].items():
            merged["stats"][k] = merged["stats"].get(k, 0) + v
    # Wallets: each part starts at START_BALANCE; add up each part's change.
    for key in ("final_balance_lamports", "equity_lamports"):
        merged[key] = {
            name: START_BALANCE + sum(s[key][name] - START_BALANCE for s in summaries)
            for name in first[key]
        }
    merged["open_value_lamports"] = {
        name: sum(s["open_value_lamports"][name] for s in summaries)
        for name in first["open_value_lamports"]
    }
    (out / "summary.json").write_text(json.dumps(merged, indent=1))
    shutil.rmtree(out / "parts")
    return merged


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", type=_day, help="first UTC day (default: all)")
    parser.add_argument("--to", dest="end", type=_day, help="stop before this UTC day")
    parser.add_argument("--out", type=Path, help="run folder (default data/backtest/<now>)")
    parser.add_argument("--max-stake-sol", type=float, default=1.0)
    parser.add_argument("--jobs", type=int, default=1, help="parts run side by side")
    parser.add_argument("--grid", choices=sorted(GRIDS), default="default", help="variant set")
    args = parser.parse_args(argv)
    out = args.out or BACKTEST / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.jobs > 1:
        lo, hi = data_span()
        start, end = args.start or lo, args.end or hi
        merged = run_parallel(out, start, end, args.jobs, stake=args.max_stake_sol, grid=args.grid)
        print(f"{out}: {merged['closed_trades']:,} closed trades in {args.jobs} parts")
    else:
        engine = run(out, args.start, args.end, max_stake_sol=args.max_stake_sol, grid=args.grid)
        pnl = to_sol(engine.closed_pnl_lamports)
        print(f"{out}: {engine.closed_trades:,} closed trades, {pnl:+.4f} SOL in total")
    print(f"report: uv run python -m cryptobot.m6_report {out}")


if __name__ == "__main__":
    main()
