"""Copy trading screen: follow wallets that made money before, with our own delay.

A different bet from dip buying: instead of reading the chart, ride with traders who
have been right before. The test is strictly out of sample:

1. Leaders are picked on 1-23 Sep only (tracked.positions and tracked.score: closed
   positions on several tokens, a win rate of at least half, up overall, no bots,
   snipers or creators). `top50` is the 50 most profitable of them.
2. From 24 Sep on, when a leader buys a classic token at most an hour old, we decide
   to buy at the moment that trade reaches us; the order lands 2 s later (screen.py's
   fill model, checked against replay.py). `copy_first` follows the first leader in a
   token; `copy_two` waits until a second leader has bought.
3. Exits: sell when a leader we followed sells (`leader`), alone or with a 30% stop, or
   the screen's target, stop and trailing exits; all with a 60-minute time limit.

Usage: uv run python -m cryptobot.follow [--jobs 4] then --report <file>.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import polars as pl

from cryptobot import tracked
from cryptobot.screen import (
    DEFAULT_PARAMS,
    DISCOVERY_END,
    FOLLOW_FILES,
    MINUTE,
    Exit,
    Params,
    _classic_creates,
    _pct,
    _summary,
    columns,
    simulate,
)
from cryptobot.slinky import SLINKY_ROOT, files

DERIVED = SLINKY_ROOT.parent.parent / "derived"
OUT = DERIVED / "follow.parquet"
LEADERS = DERIVED / "follow_leaders.parquet"
MAX_AGE_MS = 60 * MINUTE
TOP = 50

EXITS = (
    Exit("leader", leader=True),
    Exit("leader_sl30", stop=0.30, leader=True),
    Exit("tp25_sl15", target=0.25, stop=0.15),
    Exit("tp50_sl20", target=0.50, stop=0.20),
    Exit("trail25_sl30", stop=0.30, trail=0.25),
)


def build_leaders(root: Path = SLINKY_ROOT, out: Path = LEADERS) -> pl.DataFrame:
    """Score every wallet on trades before DISCOVERY_END; keep the tracked ones."""
    trades, creates = tracked.load_slinky(root)
    cut = int(DISCOVERY_END.timestamp() * 1000)
    trades = trades.filter(pl.col("ts_ms") < cut)
    creates = creates.filter(pl.col("block_time").dt.epoch("ms") < cut)
    pos = tracked.positions(trades, creates)
    scored = tracked.score(pos, creates).filter("tracked").sort("pnl", descending=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    scored.write_parquet(out)
    meta = {"wallets": scored.height, "positions": pos.height, "until": DISCOVERY_END.isoformat()}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return scored


def follow_token(
    ts: np.ndarray,
    vsol: np.ndarray,
    vtok: np.ndarray,
    users: np.ndarray,
    is_buy: np.ndarray,
    created_ms: int,
    lists: dict[str, frozenset[str]],
    p: Params = DEFAULT_PARAMS,
) -> list[tuple]:
    rows: list[tuple] = []
    young = (ts - created_ms) <= MAX_AGE_MS
    for tag, leaders in lists.items():
        is_leader = np.fromiter((u in leaders for u in users), bool, users.size)
        buys = np.flatnonzero(is_leader & is_buy & young)
        if buys.size == 0:
            continue
        first = int(buys[0])
        sells = is_leader & ~is_buy & (users == users[first])
        rows += simulate(ts, vsol, vtok, created_ms, exits=EXITS, p=p,
                         starts=[(f"copy_first_{tag}", first)], leader_sell=sells)  # fmt: skip
        distinct: list[str] = []
        for b in buys:
            if users[b] not in distinct:
                distinct.append(users[b])
            if len(distinct) == 2:
                both = np.isin(users, distinct) & ~is_buy
                start = [(f"copy_two_{tag}", int(b))]
                rows += simulate(ts, vsol, vtok, created_ms, exits=EXITS, p=p,
                                 starts=start, leader_sell=both)  # fmt: skip
                break
    return rows


def _chunk(args: tuple[int, list[tuple], dict[str, frozenset[str]], Params]) -> pl.DataFrame:
    k, chunks, lists, p = args
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    _, create_path, _ = chunks[k]
    cut = int(DISCOVERY_END.timestamp() * 1000)
    creates = _classic_creates(create_path).filter(pl.col("created_ms") >= cut)
    if creates.is_empty():
        return pl.DataFrame()
    trade_paths = [c[2] for c in chunks[k : k + 1 + FOLLOW_FILES]]
    trades = (
        pl.scan_parquet(trade_paths)
        .select(
            "mint",
            "slot",
            "tx_index",
            "virtual_sol_reserves",
            "virtual_token_reserves",
            "trade_user",
            "signature",
            is_buy=pl.col("ix_name") != "sell",
            ts=pl.col("ingested_at").dt.epoch("ms"),
        )
        .join(creates.lazy().select("mint"), on="mint", how="semi")
        .filter(pl.col("virtual_sol_reserves") > 0)
        .unique(maintain_order=False)
        .sort(["mint", "slot", "tx_index"])
        .with_columns(pl.col("ts").cum_max().over("mint"))
        .collect()
    )
    created = dict(creates.iter_rows())
    mints = trades["mint"].to_numpy()
    names = ("ts", "virtual_sol_reserves", "virtual_token_reserves", "trade_user", "is_buy")
    cols = {c: trades[c].to_numpy() for c in names}
    cuts = np.flatnonzero(mints[1:] != mints[:-1]) + 1
    bounds = np.concatenate(([0], cuts, [mints.size]))
    rows: list[tuple] = []
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        if a == b:
            continue
        mint = mints[a]
        got = follow_token(
            cols["ts"][a:b],
            cols["virtual_sol_reserves"][a:b],
            cols["virtual_token_reserves"][a:b],
            cols["trade_user"][a:b],
            cols["is_buy"][a:b],
            created[mint],
            lists,
            p,
        )
        rows += [(mint, *r) for r in got]
    print(f"{create_path.name}: {len(created):,} launches, {len(rows):,} trades", flush=True)
    return pl.DataFrame(rows, schema=["mint", *columns()], orient="row")


def run(out: Path, jobs: int, root: Path = SLINKY_ROOT, p: Params = DEFAULT_PARAMS):
    if not LEADERS.exists():
        build_leaders(root)
    board = pl.read_parquet(LEADERS)
    users = board["user"].to_list()
    lists = {"tracked": frozenset(users), f"top{TOP}": frozenset(users[:TOP])}
    chunks = files(root)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(jobs, mp_context=ctx) as pool:
        parts = list(pool.map(_chunk, [(k, chunks, lists, p) for k in range(len(chunks))]))
    df = pl.concat([d for d in parts if d.height], how="vertical_relaxed")
    df = df.unique(["mint", "entry", "exit", "venue"], keep="first")
    df.write_parquet(out)
    return df


def report(df: pl.DataFrame, leaders: dict) -> str:
    lines = [
        "# Copy trading screen: following past winners",
        "",
        "Generated by `uv run python -m cryptobot.follow --report`. Leaders picked on "
        f"trades before {DISCOVERY_END:%d %b %Y} ({leaders.get('wallets', '?'):,} wallets "
        "passed `tracked.score`); every trade below is on a token launched after that, "
        "so the whole table is out of sample. Stake 0.25 SOL, 2 s delay, fees included, "
        "network costs not. With this many rows, one with a 95% interval just above zero "
        "could still be luck.",
        "",
    ]
    for venue in df["venue"].unique().sort():
        s = _summary(df.filter(pl.col("venue") == venue), ["entry", "exit"])
        lines += [
            f"## {venue}",
            "",
            "| Entry | Exit | Trades | Win | Mean [95% CI] | Median | Graduated |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in s.sort("mean", descending=True).iter_rows(named=True):
            lines.append(
                f"| {r['entry']} | {r['exit']} | {r['n']:,} | {_pct(r['win'])} | "
                f"{_pct(r['mean'])} [{_pct(r['mean'] - r['half'])} to "
                f"{_pct(r['mean'] + r['half'])}] | {_pct(r['median'])} | {_pct(r['grad'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--leaders", action="store_true", help="only (re)build the leader list")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report:
        meta = LEADERS.with_suffix(".json")
        leaders = json.loads(meta.read_text()) if meta.exists() else {}
        print(report(pl.read_parquet(args.report), leaders))
        return
    if args.leaders:
        board = build_leaders()
        print(f"{LEADERS}: {board.height:,} leaders")
        return
    df = run(args.out, args.jobs)
    print(f"{args.out}: {df.height:,} rows")


if __name__ == "__main__":
    main()
