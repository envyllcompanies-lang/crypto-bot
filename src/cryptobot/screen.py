"""Strategy screen: many entry and exit rules over every Slinky21 token, fast.

The dip-buy family lost after fees in every form we tried (backtest and live). This
screen tests other families on the same five weeks of history before any of them goes
to paper trading:

- `mom`: momentum. Buy the first time a token's curve progress reaches p, if it got
  there within a minutes of launch (it is running, not dipping).
- `late`: buy the first time progress reaches 80% or 90%, at any age up to an hour,
  and try to ride the last stretch to graduation.
- `launch`: buy every token 15 seconds after launch. The base rate, for comparison.

Exits: take profit and stop pairs, a trailing stop, and `ride` (sell once progress
reaches 95%, before the curve completes), each with a 60-minute time limit.

Fills follow replay.py: a decision lands `delay` ms later, after every recorded trade
stamped up to then; our buy and sell fill against the curve at that point, fees come
off the SOL side, and a position still open when the curve completes is sold into the
final curve state and flagged `graduated`. Later trades replay on the curve our buy
moved, by the SOL or tokens they put in. tests/test_screen.py checks the screen against
replay.replay with the equivalent strategy (strategies.Breakout). Time exits differ
slightly: the screen decides exactly at the time limit, replay at its next 5 s tick.

Time: `received_ms` is the dataset's ingest time, made non-decreasing per token (the
backtest makes it non-decreasing across all tokens; the difference is small).
Trades inside one transaction keep their listed order (slinky.chain_order is skipped).
Network costs are not included, as in the paper reports.

Usage: uv run python -m cryptobot.screen [--jobs 4] [--out data/derived/screen]
then:  uv run python -m cryptobot.screen --report data/derived/screen
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from cryptobot.costs import DIRECT, TERMINAL_REFERRAL, TOKEN_ACCOUNT_RENT_LAMPORTS, Venue
from cryptobot.curve import (
    CURVE_SALE_TOKENS,
    GRADUATION_VIRTUAL_TOKENS,
    INITIAL_VIRTUAL_SOL,
    INITIAL_VIRTUAL_TOKENS,
    TOTAL_SUPPLY,
    Curve,
    to_lamports,
)
from cryptobot.slinky import SLINKY_ROOT, files

OUT = SLINKY_ROOT.parent.parent / "derived" / "screen"  # one parquet part per create file
MINUTE = 60_000
FOLLOW_FILES = 2  # a token's trades are read from its create file and the next two (36 h)
DISCOVERY_END = datetime(2026, 9, 24, tzinfo=UTC)  # rules are picked before, judged after


@dataclass(frozen=True)
class Entry:
    name: str
    progress: float | None  # buy when progress first reaches this; None = by age only
    max_age_ms: int  # ...if the token is at most this old then
    min_age_ms: int = 0  # `launch`: the first trade at or after this age


@dataclass(frozen=True)
class Exit:
    name: str
    target: float | None = None
    stop: float | None = None
    trail: float | None = None
    ride: float | None = None  # sell once progress reaches this
    timeout_ms: int = 60 * MINUTE
    leader: bool = False  # copy trading: sell when the wallet we followed sells


ENTRIES = (
    *(
        Entry(f"mom_p{round(p * 100)}_a{a}", p, a * MINUTE)
        for p in (0.10, 0.25, 0.50)
        for a in (1, 3, 10)
    ),
    Entry("late_p80", 0.80, 60 * MINUTE),
    Entry("late_p90", 0.90, 60 * MINUTE),
    Entry("launch_15s", None, 60 * MINUTE, min_age_ms=15_000),
)
EXITS = (
    Exit("tp25_sl15", target=0.25, stop=0.15),
    Exit("tp50_sl20", target=0.50, stop=0.20),
    Exit("tp100_sl30", target=1.00, stop=0.30),
    Exit("trail25_sl30", stop=0.30, trail=0.25),
    Exit("ride95_sl30", stop=0.30, ride=0.95),
    Exit("ride95_trail30", trail=0.30, ride=0.95),
)
VENUES = (TERMINAL_REFERRAL, DIRECT)

# Timing map (`--timing`): buy when progress first reaches each level, hold a fixed time,
# with no fees as well as direct fees, to see whether any entry moment has positive drift.
NO_FEES = Venue("No fees (price move only)", 0.0)
TIMING_LEVELS = tuple(round(0.05 * k, 2) for k in range(1, 20))
TIMING_ENTRIES = tuple(Entry(f"p{round(p * 100):02d}", p, 60 * MINUTE) for p in TIMING_LEVELS)
TIMING_EXITS = tuple(Exit(f"hold{m}m", timeout_ms=m * MINUTE) for m in (1, 5, 15, 60))
TIMING_VENUES = (NO_FEES, DIRECT)


@dataclass(frozen=True)
class Params:
    stake_lamports: int = to_lamports(0.25)
    delay_ms: int = 2_000
    rent_lamports: int = TOKEN_ACCOUNT_RENT_LAMPORTS


DEFAULT_PARAMS = Params()


def _fee(lamports: int, venue: Venue) -> int:
    return -(-int(lamports * venue.fee_bps_per_side) // 10_000)


def entry_index(e: Entry, ts: np.ndarray, prog: np.ndarray, created_ms: int) -> int:
    """Index of the trade that triggers the buy decision, or -1."""
    age = ts - created_ms
    if e.progress is None:
        hit = np.flatnonzero(age >= e.min_age_ms)
    else:
        hit = np.flatnonzero(prog >= e.progress)
    if hit.size == 0:
        return -1
    i = int(hit[0])
    return i if age[i] <= e.max_age_ms else -1


def simulate(
    ts: np.ndarray,
    vsol: np.ndarray,
    vtok: np.ndarray,
    created_ms: int,
    entries: tuple[Entry, ...] = ENTRIES,
    exits: tuple[Exit, ...] = EXITS,
    venues: tuple[Venue, ...] = VENUES,
    p: Params = DEFAULT_PARAMS,
    starts: list[tuple[str, int]] | None = None,
    leader_sell: np.ndarray | None = None,
) -> list[tuple]:
    """Every (entry, exit, venue) trade on one token: rows for `columns()`.

    `starts` replaces the entry rules with given (name, decision index) pairs, and
    `leader_sell` marks the trades where a followed wallet sold (copy trading).
    """
    n = ts.size
    if n == 0:
        return []
    prog = (INITIAL_VIRTUAL_TOKENS - vtok) / CURVE_SALE_TOKENS
    grad_hits = np.flatnonzero(vtok <= GRADUATION_VIRTUAL_TOKENS)
    grad = int(grad_hits[0]) if grad_hits.size else n  # trades after it never happen
    horizon = max(x.timeout_ms for x in exits) + 2 * p.delay_ms
    out: list[tuple] = []
    if starts is None:
        starts = [(e.name, entry_index(e, ts[:grad], prog[:grad], created_ms)) for e in entries]
    for name, i in starts:
        if i < 0 or i >= grad:
            continue
        land = int(ts[i]) + p.delay_ms
        j = int(np.searchsorted(ts, land, side="right")) - 1
        if j >= grad:
            continue  # the curve completed before our buy landed
        end = min(int(np.searchsorted(ts, land + horizon, side="right")) - 1, grad, n - 1)
        for venue in venues:
            to_curve = int(p.stake_lamports / (1 + venue.fee_rate))
            after, tokens, spent = Curve(int(vsol[j]), int(vtok[j])).after_buy(to_curve)
            if tokens == 0:
                continue
            cost = spent + _fee(spent, venue) + p.rent_lamports
            s_path, t_path = moved_path(vsol, vtok, j, end, after)
            for x in exits:
                out.append(
                    _exit_row(
                        name,
                        x,
                        venue,
                        p,
                        ts,
                        grad,
                        n,
                        i,
                        land,
                        tokens,
                        cost,
                        spent / tokens,
                        after,
                        s_path,
                        t_path,
                        created_ms,
                        float(prog[j]),
                        leader_sell,
                    )
                )
    return out


def moved_path(
    vsol: np.ndarray, vtok: np.ndarray, j: int, end: int, ours: Curve
) -> tuple[np.ndarray, np.ndarray]:
    """Reserves after each recorded trade j+1..end, replayed on the curve we moved.

    As in replay.py: buys go in by the SOL that reached the curve, sells by the tokens
    sold, both read off the recorded reserve changes.
    """
    s, t = ours.virtual_sol, ours.virtual_tokens
    rec_s = vsol[j : end + 1].tolist()
    rec_t = vtok[j : end + 1].tolist()
    out_s, out_t = [], []
    for m in range(1, len(rec_s)):
        ds = rec_s[m] - rec_s[m - 1]
        if ds >= 0:  # a buy: ds lamports in
            got = ds * t // (s + ds)
            got = min(got, max(t - GRADUATION_VIRTUAL_TOKENS, 0))
            s, t = s + ds, t - got
        else:  # a sell: tokens in
            dt = rec_t[m] - rec_t[m - 1]
            s, t = s - dt * s // (t + dt), t + dt
        out_s.append(s)
        out_t.append(t)
    return np.array(out_s, dtype=np.int64), np.array(out_t, dtype=np.int64)


def _exit_row(name, x, venue, p, ts, grad, n, i, land_buy, tokens, cost, entry_price, after,
              s_path, t_path, created_ms, entry_progress, leader_sell=None) -> tuple:  # fmt: skip
    """Find the exit decision on the path after our buy, then fill it."""
    base = int(np.searchsorted(ts, land_buy, side="right"))  # first trade after our buy
    high0 = after.price()
    price = s_path / t_path if s_path.size else s_path.astype(np.float64)
    reasons = []
    if x.target is not None:
        reasons.append(("target", price >= entry_price * (1 + x.target)))
    if x.stop is not None:
        reasons.append(("stop", price < entry_price * (1 - x.stop)))
    if x.trail is not None:
        high = np.maximum.accumulate(np.maximum(price, high0)) if price.size else price
        reasons.append(("trail", price <= high * (1 - x.trail)))
    if x.ride is not None:
        moved_prog = (INITIAL_VIRTUAL_TOKENS - t_path) / CURVE_SALE_TOKENS
        reasons.append(("ride", moved_prog >= x.ride))
    if x.leader and leader_sell is not None:
        reasons.append(("leader", leader_sell[base : base + price.size].copy()))
    # Graduation: the completing trade gets no decision (replay breaks before deciding).
    if grad < n and price.size:
        cut = grad - base
        for _, hit in reasons:
            hit[cut:] = False
    first, why = price.size, ""
    for reason, hit in reasons:  # priority order as in DipBuyer: target, stop, trail
        idx = np.flatnonzero(hit[:first])
        if idx.size:
            first, why = int(idx[0]), reason
    timeout_at = land_buy + x.timeout_ms
    if first < price.size and ts[base + first] <= timeout_at:
        decided = int(ts[base + first])
    else:
        decided, why = timeout_at, "timeout"
    land = decided + p.delay_ms
    jj = int(np.searchsorted(ts, land, side="right")) - 1
    flag = ""
    if grad < n and jj >= grad:
        jj, flag, why = grad, "graduated", "graduated"
    m = jj - base  # position in the moved path; -1 is the state right after our buy
    state = after if m < 0 else Curve(int(s_path[m]), int(t_path[m]))
    _, gross = state.after_sell(tokens)
    proceeds = gross - _fee(gross, venue) + p.rent_lamports
    return (
        name,
        x.name,
        venue.name,
        created_ms,
        int(ts[i]),
        land_buy,
        land,
        why,
        flag,
        cost,
        proceeds,
        (proceeds - cost) / cost,
        entry_progress,
    )


def columns() -> list[str]:
    return [
        "entry",
        "exit",
        "venue",
        "created_ms",
        "decided_ms",
        "entry_ms",
        "exit_ms",
        "exit_reason",
        "flag",
        "cost_lamports",
        "proceeds_lamports",
        "net_return",
        "entry_progress",
    ]


# Data -------------------------------------------------------------------------------


def _classic_creates(path: Path) -> pl.DataFrame:
    """Pump.fun launches on a SOL curve at 30 virtual SOL, 1B supply, no Mayhem mode."""
    return (
        pl.scan_parquet(path)
        .filter(
            (pl.col("virtual_sol_reserves") == INITIAL_VIRTUAL_SOL)
            & (pl.col("virtual_token_reserves") == INITIAL_VIRTUAL_TOKENS)
            & (pl.col("token_total_supply") == TOTAL_SUPPLY)
            & ~pl.col("is_mayhem_mode").fill_null(False)
        )
        .select("mint", created_ms=pl.col("ingested_at").dt.epoch("ms"))
        .unique("mint", keep="first")
        .collect()
    )


def _chunk(args: tuple) -> int:
    """Simulate one create file's tokens into out/part-<k>.parquet; skip if it exists."""
    k, chunks, p, out, timing = args
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    _, create_path, _ = chunks[k]
    part = out / f"part-{k:03d}.parquet"
    if part.exists():
        return -1
    creates = _classic_creates(create_path)
    trade_paths = [c[2] for c in chunks[k : k + 1 + FOLLOW_FILES]]
    trades = (
        pl.scan_parquet(trade_paths)
        .select(
            "mint",
            "slot",
            "tx_index",
            "virtual_sol_reserves",
            "virtual_token_reserves",
            ts=pl.col("ingested_at").dt.epoch("ms"),
        )
        .join(creates.lazy().select("mint"), on="mint", how="semi")
        .filter(pl.col("virtual_sol_reserves") > 0)
        .unique(maintain_order=False)  # files overlap a little at the edges
        .sort(["mint", "slot", "tx_index"])
        .with_columns(pl.col("ts").cum_max().over("mint"))
        .collect()
    )
    created = dict(creates.iter_rows())
    mints = trades["mint"].to_numpy()
    ts = trades["ts"].to_numpy()
    vsol = trades["virtual_sol_reserves"].to_numpy()
    vtok = trades["virtual_token_reserves"].to_numpy()
    cuts = np.flatnonzero(mints[1:] != mints[:-1]) + 1
    bounds = np.concatenate(([0], cuts, [mints.size]))
    rows: list[tuple] = []
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        if a == b:
            continue
        mint = mints[a]
        grid = (TIMING_ENTRIES, TIMING_EXITS, TIMING_VENUES) if timing else (ENTRIES, EXITS, VENUES)
        for r in simulate(ts[a:b], vsol[a:b], vtok[a:b], created[mint], *grid, p=p):
            rows.append((mint, *r))
    print(f"{create_path.name}: {len(created):,} launches, {len(rows):,} trades", flush=True)
    write_part(rows, part)
    return len(rows)


def write_part(rows: list[tuple], part: Path) -> None:
    """Rows to a parquet part, written under a temporary name and then renamed."""
    s, i, f = pl.String, pl.Int64, pl.Float64
    types = [s, s, s, s, i, i, i, i, s, s, i, i, f, f]
    schema = dict(zip(["mint", *columns()], types, strict=True))
    df = pl.DataFrame(rows, schema=schema, orient="row")
    tmp = part.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.rename(part)


def run(
    out: Path,
    jobs: int,
    root: Path = SLINKY_ROOT,
    p: Params = DEFAULT_PARAMS,
    timing: bool = False,
) -> int:
    """Every create file into its own part under `out`; a rerun resumes where it stopped."""
    chunks = files(root)
    out.mkdir(parents=True, exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(jobs, mp_context=ctx) as pool:
        work = [(k, chunks, p, out, timing) for k in range(len(chunks))]
        counts = list(pool.map(_chunk, work))
    return sum(c for c in counts if c > 0)


def load(out: Path) -> pl.DataFrame:
    """The result parts, with only the columns the reports use, deduplicated.

    Create files overlap a little at their edges, so a token can be simulated twice.
    """
    cols = ["mint", "entry", "exit", "venue", "created_ms", "exit_reason", "flag", "net_return"]
    cat = ["entry", "exit", "venue", "exit_reason", "flag"]
    return (
        pl.scan_parquet(sorted(out.glob("part-*.parquet")))
        .select(cols)
        .with_columns(pl.col(cat).cast(pl.Categorical))
        .unique(["mint", "entry", "exit", "venue"], keep="first")
        .drop("mint")
        .collect()
    )


# Report ------------------------------------------------------------------------------


def _summary(df: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    return (
        df.group_by(by)
        .agg(
            n=pl.len(),
            win=(pl.col("net_return") > 0).mean(),
            mean=pl.col("net_return").mean(),
            median=pl.col("net_return").median(),
            sd=pl.col("net_return").std(),
            grad=(pl.col("flag") == "graduated").mean(),
        )
        .with_columns(half=1.959964 * pl.col("sd").fill_null(0) / pl.col("n").sqrt())
    )


def _pct(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def report(df: pl.DataFrame, min_n: int = 200, delay_ms: int = 2_000) -> str:
    cut = int(DISCOVERY_END.timestamp() * 1000)
    df = df.with_columns(period=pl.when(pl.col("created_ms") < cut).then(pl.lit("discovery"))
                         .otherwise(pl.lit("holdout")))  # fmt: skip
    first = datetime.fromtimestamp(df["created_ms"].min() / 1000, UTC)
    last = datetime.fromtimestamp(df["created_ms"].max() / 1000, UTC)
    lines = [
        "# Strategy screen: momentum, late-curve and base-rate entries",
        "",
        "Generated by `uv run python -m cryptobot.screen --report`. Slinky21 history, "
        f"launches {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC; classic curves "
        "only (SOL-quoted, 30 virtual SOL, no Mayhem). Stake 0.25 SOL, "
        f"{delay_ms / 1000:g} s delay, fees "
        "included, network costs not. One trade per token per rule.",
        "",
        f"Discovery: launches before {DISCOVERY_END:%d %b}. Holdout: from then on. The "
        f"pre-set rule: the best discovery mean among rules with at least {min_n} trades "
        "is judged on the holdout; it passes only if the holdout mean and its 95% lower "
        "bound are above zero.",
        "",
    ]
    for venue in df["venue"].unique().sort():
        v = df.filter(pl.col("venue") == venue)
        s = _summary(v, ["entry", "exit", "period"])
        disc = s.filter((pl.col("period") == "discovery") & (pl.col("n") >= min_n))
        hold = s.filter(pl.col("period") == "holdout")
        joined = disc.join(hold, on=["entry", "exit"], suffix="_h", how="left").sort(
            "mean", descending=True
        )
        positive = joined.filter(pl.col("mean") > 0).height
        lines += [
            f"## {venue}",
            "",
            f"{joined.height} rules with at least {min_n} discovery trades; {positive} with "
            "a positive discovery mean.",
            "",
            "| Entry | Exit | Disc. n | Disc. win | Disc. mean [95% CI] | Hold. n | Hold. win "
            "| Hold. mean [95% CI] | Graduated |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in joined.head(15).iter_rows(named=True):
            hold_cells = (
                [f"{r['n_h']:,}", _pct(r["win_h"]),
                 f"{_pct(r['mean_h'])} [{_pct(r['mean_h'] - r['half_h'])} to "
                 f"{_pct(r['mean_h'] + r['half_h'])}]"]
                if r["n_h"] is not None else ["0", "n/a", "n/a"]
            )  # fmt: skip
            lines.append(
                f"| {r['entry']} | {r['exit']} | {r['n']:,} | {_pct(r['win'])} | "
                f"{_pct(r['mean'])} [{_pct(r['mean'] - r['half'])} to "
                f"{_pct(r['mean'] + r['half'])}] | "
                + " | ".join(hold_cells)
                + f" | {_pct(r['grad'])} |"
            )
        if joined.height:
            best = joined.row(0, named=True)
            ok = best["n_h"] is not None and best["mean_h"] - best["half_h"] > 0
            lines += [
                "",
                f"Pre-set test: best discovery rule `{best['entry']} {best['exit']}` -> "
                + ("**pass**" if ok else "**fail**")
                + (f" (holdout mean {_pct(best['mean_h'])}, n {best['n_h']:,})."
                   if best["n_h"] is not None else " (no holdout trades)."),
                "",
            ]  # fmt: skip
        whole = _summary(v, ["entry"]).sort("mean", descending=True)
        lines += [
            "Best exit aside, each entry's average over all its exits (whole period):",
            "",
            "| Entry | Trades | Win | Mean | Median |",
            "|---|---|---|---|---|",
        ]
        for r in whole.iter_rows(named=True):
            lines.append(
                f"| {r['entry']} | {r['n']:,} | {_pct(r['win'])} | {_pct(r['mean'])} | "
                f"{_pct(r['median'])} |"
            )
        lines.append("")
    return "\n".join(lines)


POOLS = SLINKY_ROOT.parent / "loopholetape__pumpfun-launches/data/graduation_pools"


def pools_summary(root: Path = POOLS) -> str:
    """What PumpSwap pools did in the 30 minutes after graduation (loopholetape data)."""
    d = pl.read_parquet(root / "graduation_pools.parquet")
    r = d.filter(pl.col("complete") == 1)["price_ratio_30m"].drop_nulls()
    first = datetime.fromtimestamp(d["open_t"].min(), UTC)
    last = datetime.fromtimestamp(d["open_t"].max(), UTC)
    collapsed = d["collapsed_t"].drop_nulls()
    return "\n".join(
        [
            f"Pools opened {first:%Y-%m-%d} to {last:%Y-%m-%d}: {d.height:,}; "
            f"complete 30-minute captures with a price ratio: {r.len():,}",
            f"Median price at 30 min / opening price: {r.median():.3f}",
            f"Below half the opening price: {_pct((r < 0.5).mean())}; "
            f"above it: {_pct((r > 1).mean())}; above 2x: {_pct((r > 2).mean())}",
            f"Fell to 10% of the opening price within 30 min: {collapsed.len():,} of "
            f"{d.height:,}, median {collapsed.median():.0f} s after opening",
        ]
    )


def timing_report(df: pl.DataFrame, delay_ms: int) -> str:
    """Mean return by entry level and holding time, without and with fees."""
    s = _summary(df, ["entry", "exit", "venue"])
    lines = [
        "# Timing map: does price rise after any entry moment?",
        "",
        "Generated by `uv run python -m cryptobot.screen --timing --report`. Every classic "
        "launch in the Slinky21 history; buy 0.25 SOL when curve progress first reaches "
        f"each level (token at most 60 minutes old), {delay_ms / 1000:g} s delay, hold a "
        "fixed time, sell. 'No fees' is the price move alone, our own price impact "
        "included; 'direct' adds Pump.fun's 1.25% a side. Positions still open at "
        "graduation are valued at the final curve price (optimistic).",
        "",
    ]
    exits = [x.name for x in TIMING_EXITS]
    for venue in (NO_FEES.name, DIRECT.name):
        lines += [f"## {venue}: mean return per trade", "", "| Level | Trades | "
                  + " | ".join(exits) + " |", "|---|---|" + "---|" * len(exits)]  # fmt: skip
        v = s.filter(pl.col("venue") == venue)
        for e in sorted(v["entry"].unique().to_list()):
            row = v.filter(pl.col("entry") == e)
            cells = []
            for x in exits:
                r = row.filter(pl.col("exit") == x)
                if r.height:
                    m, h = r["mean"][0], r["half"][0]
                    cells.append(f"{_pct(m)} [{_pct(m - h)}, {_pct(m + h)}]")
                else:
                    cells.append("n/a")
            lines.append(f"| {e} | {row['n'].max():,} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--delay-ms", type=int, default=2_000)
    parser.add_argument("--report", type=Path, help="write the report for this result file")
    parser.add_argument("--pools", action="store_true", help="post-graduation pool summary")
    parser.add_argument("--timing", action="store_true", help="the timing map grid")
    args = parser.parse_args(argv)
    if args.pools:
        print(pools_summary())
        return
    if args.report:
        df = load(args.report)
        if args.timing:
            print(timing_report(df, args.delay_ms))
        else:
            print(report(df, delay_ms=args.delay_ms))
        return
    rows = run(args.out, args.jobs, p=Params(delay_ms=args.delay_ms), timing=args.timing)
    print(f"{args.out}: {rows:,} new rows")
    print(f"report: uv run python -m cryptobot.screen --report {args.out}")


if __name__ == "__main__":
    main()
