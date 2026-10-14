"""Copy trading screen: follow wallets that made money before, with our own delay.

A different bet from dip buying: instead of reading the chart, ride with traders who
have been right before. The test is strictly out of sample:

1. Leaders are picked on 1-23 Sep only. `tracked`: tracked.score's rule (closed
   positions on several tokens, a win rate of at least half, up overall, no bots,
   snipers or creators); `top50` is the 50 most profitable of them. `copyable`: wallets
   whose own buys, copied 2 s late through the fill model and sold when they sold,
   made money after fees on at least 15 tokens (mean and median above zero). Ranking
   by what a follower would have made, not by the wallet's own profit, follows the
   research advice in reports/2026-10-14-strategy-research.md.
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
from cryptobot.costs import TERMINAL_REFERRAL
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
    load,
    simulate,
    write_part,
)
from cryptobot.slinky import SLINKY_ROOT, files

DERIVED = SLINKY_ROOT.parent.parent / "derived"
OUT = DERIVED / "follow"
LEADERS = DERIVED / "follow_leaders.parquet"
MAX_AGE_MS = 60 * MINUTE
TOP = 50
MIN_COPIED = 15  # copied trades a wallet needs in discovery to make the copyable list
VENUES_SCORE = (TERMINAL_REFERRAL,)

EXITS = (
    Exit("leader", leader=True),
    Exit("leader_sl30", stop=0.30, leader=True),
    Exit("tp25_sl15", target=0.25, stop=0.15),
    Exit("tp50_sl20", target=0.50, stop=0.20),
    Exit("trail25_sl30", stop=0.30, trail=0.25),
)


def build_leaders(root: Path = SLINKY_ROOT, out: Path = LEADERS) -> pl.DataFrame:
    """Score every wallet on trades before DISCOVERY_END.

    Keeps the tracked wallets and, as candidates for the copyable list, every other
    wallet that is not a bot and closed at least MIN_COPIED positions at a profit.
    """
    trades, creates = tracked.load_slinky(root)
    cut = int(DISCOVERY_END.timestamp() * 1000)
    trades = trades.filter(pl.col("ts_ms") < cut)
    creates = creates.filter(pl.col("block_time").dt.epoch("ms") < cut)
    pos = tracked.positions(trades, creates)
    scored = (
        tracked.score(pos, creates)
        .filter(
            pl.col("tracked")
            | (~pl.col("bot") & (pl.col("closed") >= MIN_COPIED) & (pl.col("pnl") > 0))
        )
        .sort("pnl", descending=True)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    scored.write_parquet(out)
    meta = {
        "wallets": int(scored["tracked"].sum()),
        "candidates": scored.height,
        "positions": pos.height,
        "until": DISCOVERY_END.isoformat(),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return scored


def copy_each_leader(
    ts: np.ndarray,
    vsol: np.ndarray,
    vtok: np.ndarray,
    users: np.ndarray,
    is_buy: np.ndarray,
    created_ms: int,
    candidates: frozenset[str],
    p: Params = DEFAULT_PARAMS,
) -> list[tuple]:
    """Copy every candidate's first buy on this token, selling when it sells.

    The entry name is the wallet, so results can be grouped into each wallet's
    copyable return: what a follower 2 s behind would have made, after fees.
    """
    rows: list[tuple] = []
    young = (ts - created_ms) <= MAX_AGE_MS
    seen: set[str] = set()
    for b in np.flatnonzero(is_buy & young):
        u = users[b]
        if u in seen or u not in candidates:
            continue
        seen.add(u)
        sells = (users == u) & ~is_buy
        rows += simulate(ts, vsol, vtok, created_ms, exits=EXITS[:1], venues=VENUES_SCORE,
                         p=p, starts=[(u, int(b))], leader_sell=sells)  # fmt: skip
    return rows


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


def _chunk(args: tuple) -> int:
    """One create file's tokens: `score` copies each candidate, `follow` runs the lists."""
    k, chunks, lists, p, mode, out = args
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    _, create_path, _ = chunks[k]
    part = out / f"part-{k:03d}.parquet"
    if part.exists():
        return -1
    cut = int(DISCOVERY_END.timestamp() * 1000)
    creates = _classic_creates(create_path)
    in_period = pl.col("created_ms") < cut if mode == "score" else pl.col("created_ms") >= cut
    creates = creates.filter(in_period)
    if creates.is_empty():
        write_part([], part)
        return 0
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
        args_ = (
            cols["ts"][a:b],
            cols["virtual_sol_reserves"][a:b],
            cols["virtual_token_reserves"][a:b],
            cols["trade_user"][a:b],
            cols["is_buy"][a:b],
            created[mint],
        )
        if mode == "score":
            got = copy_each_leader(*args_, lists["candidates"], p)
        else:
            got = follow_token(*args_, lists, p)
        rows += [(mint, *r) for r in got]
    print(f"{create_path.name}: {len(created):,} launches, {len(rows):,} trades", flush=True)
    write_part(rows, part)
    return len(rows)


def _pool(jobs: int, root: Path, lists: dict, p: Params, mode: str, out: Path) -> pl.DataFrame:
    """Run every create file into parts under `out` (resumable), then load them."""
    chunks = files(root)
    out.mkdir(parents=True, exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    work = [(k, chunks, lists, p, mode, out) for k in range(len(chunks))]
    with ProcessPoolExecutor(jobs, mp_context=ctx) as pool:
        list(pool.map(_chunk, work))
    return pl.read_parquet(sorted(out.glob("part-*.parquet"))).unique(
        ["mint", "entry", "exit", "venue"], keep="first"
    )


def copyable(scores: pl.DataFrame, min_copied: int = MIN_COPIED) -> pl.DataFrame:
    """Wallets whose copied trades (2 s late, after fees) made money in discovery.

    Mean and median must both be positive, so one lucky runner can't carry a wallet.
    """
    return (
        scores.group_by(wallet=pl.col("entry"))
        .agg(
            copied=pl.len(),
            mean=pl.col("net_return").mean(),
            median=pl.col("net_return").median(),
            win=(pl.col("net_return") > 0).mean(),
        )
        .filter((pl.col("copied") >= min_copied) & (pl.col("mean") > 0) & (pl.col("median") > 0))
        .sort("mean", descending=True)
    )


def run(out: Path, jobs: int, root: Path = SLINKY_ROOT, p: Params = DEFAULT_PARAMS):
    if not LEADERS.exists():
        build_leaders(root)
    board = pl.read_parquet(LEADERS)
    users = board.filter("tracked")["user"].to_list()
    copyable_path = DERIVED / "follow_copyable.parquet"
    if copyable_path.exists():  # a rerun (e.g. another delay) keeps the list it was given
        good = pl.read_parquet(copyable_path)
        scores = pl.DataFrame({"n": []})
    else:
        candidates = {"candidates": frozenset(board["user"].to_list())}
        scores = _pool(jobs, root, candidates, p, "score", DERIVED / "follow_scores")
        good = copyable(scores)
        good.write_parquet(copyable_path)
    lists = {
        "tracked": frozenset(users),
        f"top{TOP}": frozenset(users[:TOP]),
        "copyable": frozenset(good["wallet"].to_list()),
    }
    meta = json.loads(LEADERS.with_suffix(".json").read_text())
    meta |= {"copyable": good.height}
    if scores.height:
        meta["copied_in_discovery"] = scores.height
    LEADERS.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return _pool(jobs, root, lists, p, "follow", out)


def report(df: pl.DataFrame, leaders: dict, delay_ms: int = 2_000) -> str:
    lines = [
        "# Copy trading screen: following past winners",
        "",
        "Generated by `uv run python -m cryptobot.follow --report`. Leaders picked on "
        f"trades before {DISCOVERY_END:%d %b %Y} ({leaders.get('wallets', '?'):,} wallets "
        "passed `tracked.score`); every trade below is on a token launched after that, "
        "so the whole table is out of sample. Stake 0.25 SOL, "
        f"{delay_ms / 1000:g} s delay, fees included, "
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
    parser.add_argument("--delay-ms", type=int, default=2_000)
    parser.add_argument("--leaders", action="store_true", help="only (re)build the leader list")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report:
        meta = LEADERS.with_suffix(".json")
        leaders = json.loads(meta.read_text()) if meta.exists() else {}
        print(report(load(args.report), leaders, args.delay_ms))
        return
    if args.leaders:
        board = build_leaders()
        print(f"{LEADERS}: {board.height:,} leaders")
        return
    df = run(args.out, args.jobs, p=Params(delay_ms=args.delay_ms))
    print(f"{args.out}: {df.height:,} rows")


if __name__ == "__main__":
    main()
