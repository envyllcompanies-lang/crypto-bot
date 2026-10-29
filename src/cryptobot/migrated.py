"""After graduation: the strategies of source video V4 (and CLAUDE.md's S5) on PumpSwap.

V4 trades coins after they graduate: it waits for the post-graduation dump, buys the
reversal off the low and sells around 5x ("bottom bidding"), or holds for days and takes
profit on the way up ("conviction holds"). The live paper trader can't test that (the
recorder only sees the bonding curve), and pumpswap.py kept only each pool's first 70
minutes. This module keeps each graduate's first three days.

**Data.** Slinky21's `swap` table (every PumpSwap swap, 277 GB) is read straight from
Hugging Face one file at a time, only the needed columns, and never saved whole: each
graduate pool's swaps within WINDOW_MS of graduation become one-minute bars of the pool's
SOL reserve (the same per-swap solution as pumpswap.pool_path: the constant product k
from the graduation deposit, each swap's input and output fixing the reserves then).
Price is s^2 / k, so the reserve's close, low and high are the price's too. Fees paid
into the pool raise k slowly over days, which this ignores: prices drift a few percent
low by day three.

Usage: uv run python -m cryptobot.migrated extract   (about 30 min for Sep-Oct)
       uv run python -m cryptobot.migrated report
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from cryptobot import pumpswap
from cryptobot.fetch import HF

OUT = pumpswap.OUT.parent / "migrated"
WINDOW_MS = 3 * 86_400_000  # each pool's first three days
MINUTE_MS = 60_000
STAMP = re.compile(r"swap_(\d{8}T\d{6})\.parquet$")


def graduates() -> pl.DataFrame:
    """Each graduate pool with an integer id, its graduation time and the deposit's k."""
    g = pumpswap.graduates().filter((pl.col("sol_amount") > 0) & (pl.col("mint_amount") > 0))
    return (
        g.sort("grad_ms")
        .with_row_index("pid")
        .select(
            pl.col("pid").cast(pl.Int32),
            "mint",
            "pool",
            "grad_slot",
            "grad_ms",
            "sol_amount",
            "mint_amount",
            k=pl.col("sol_amount").cast(pl.Float64) * pl.col("mint_amount").cast(pl.Float64),
        )
    )


def reserve_after(fee_bps: int = pumpswap.POOL_FEE_BPS) -> pl.Expr:
    """The pool's SOL reserve right after a swap, from that swap alone (pool_path)."""
    f = fee_bps / 10_000
    si, to = pl.col("sol_in").cast(pl.Float64), pl.col("tok_out").cast(pl.Float64)
    ti, so = pl.col("tok_in").cast(pl.Float64), pl.col("sol_out").cast(pl.Float64)
    k = pl.col("k")
    a = si * (1 - f)
    s_buy = (-a + (a * a + 4 * a * k / to).sqrt()) / 2 + a
    d = so / (1 - f)
    s_sell = (d * ti + (d * d * ti * ti + 4 * ti * d * k).sqrt()) / (2 * ti) - d
    return pl.when(si > 0).then(s_buy).otherwise(s_sell)


def bars(source: str | Path, grads: pl.DataFrame, window_ms: int = WINDOW_MS) -> pl.DataFrame:
    """One-minute bars of each graduate pool's SOL reserve from one swap file."""
    sol_in = pl.col("input_mint") == pumpswap.WSOL
    big = pumpswap.MIN_ANCHOR_LAMPORTS
    swaps = (
        pl.scan_parquet(source)
        .select(
            "pool", "slot", "tx_index", "input_mint", "input_amount", "output_amount",
            ts=pl.col("ingested_at").dt.epoch("ms"),
        )  # fmt: skip
        .join(grads.lazy().select("pid", "pool", "grad_slot", "grad_ms", "k"), on="pool")
        .filter(
            (pl.col("slot") >= pl.col("grad_slot"))
            & (pl.col("ts") <= pl.col("grad_ms") + window_ms)
        )
        .with_columns(
            sol_in=pl.when(sol_in).then(pl.col("input_amount")).otherwise(0),
            sol_out=pl.when(sol_in).then(0).otherwise(pl.col("output_amount")),
            tok_in=pl.when(sol_in).then(0).otherwise(pl.col("input_amount")),
            tok_out=pl.when(sol_in).then(pl.col("output_amount")).otherwise(0),
        )
        .filter(
            ((pl.col("sol_in") >= big) & (pl.col("tok_out") > 0))
            | ((pl.col("sol_out") >= big) & (pl.col("tok_in") > 0))
        )
        .with_columns(s=reserve_after())
        .filter(pl.col("s").is_finite() & (pl.col("s") > 0))
        .with_columns(
            minute=((pl.col("ts") - pl.col("grad_ms")).clip(0) // MINUTE_MS).cast(pl.Int32)
        )
    )  # fmt: skip
    return (
        swaps.sort("pid", "slot", "tx_index")
        .group_by("pid", "minute")
        .agg(
            close=pl.col("s").last().cast(pl.Float32),
            low=pl.col("s").min().cast(pl.Float32),
            high=pl.col("s").max().cast(pl.Float32),
            n=pl.len().cast(pl.Int32),
            last_slot=pl.col("slot").last(),
            last_tx=pl.col("tx_index").last(),
        )
        .collect()
    )


def extract(start: str = "2026-09-01", out: Path = OUT) -> None:
    """Every swap file from `start` on, one at a time, into out/bars-<stamp>.parquet."""
    grads = graduates()
    out.mkdir(parents=True, exist_ok=True)
    grads.write_parquet(out / "graduates.parquet")
    files = sorted(pumpswap._remote("swap"), key=lambda f: f["path"])
    for f in files:
        m = STAMP.search(f["path"])
        if m is None or m.group(1) < start.replace("-", ""):
            continue
        part = out / f"bars-{m.group(1)}.parquet"
        if part.exists():
            continue
        url = f"{HF}/datasets/{pumpswap.DATASET}/resolve/main/{f['path']}"
        t0 = time.time()
        for attempt in range(4):
            try:
                df = bars(url, grads)
                break
            except Exception as e:  # a dropped connection: try again, then give up loudly
                print(f"{part.name}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                time.sleep(10 * 2**attempt)
        else:
            raise RuntimeError(f"{f['path']} failed four times")
        tmp = part.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.rename(part)
        print(f"{part.name}: {df.height:,} bars, {df['pid'].n_unique():,} pools, "
              f"{time.time() - t0:.0f} s", flush=True)  # fmt: skip


def load(out: Path = OUT) -> tuple[pl.DataFrame, pl.DataFrame]:
    """All bars (a pool's minute split across two files is merged) and the graduates."""
    grads = pl.read_parquet(out / "graduates.parquet")
    b = pl.concat([pl.read_parquet(p) for p in sorted(out.glob("bars-*.parquet"))])
    b = (
        b.sort("pid", "minute", "last_slot", "last_tx")
        .group_by("pid", "minute", maintain_order=True)
        .agg(
            close=pl.col("close").last(),
            low=pl.col("low").min(),
            high=pl.col("high").max(),
            n=pl.col("n").sum(),
        )  # fmt: skip
        .sort("pid", "minute")
    )
    return b, grads


# The test (docs/research/2026-10-28-preregistered-after-graduation-test.md) -------------

STAKE = 250_000_000  # 0.25 SOL
FEE = pumpswap.POOL_FEE_BPS / 10_000
END_MINUTE = WINDOW_MS // MINUTE_MS - 1
DISCOVERY_END_MS = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
FIRST_MS = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
DROPS = (0.6, 0.7, 0.8)
RISES = (0.3, 1.0)
AGES = (("1-3h", 60, 180), ("30m-24h", 30, 1440))
TARGETS = (2.0, 5.0)
STAGES = ((2.0, 1 / 3), (4.0, 1 / 3))  # conviction: a third at 2x, a third at 4x
TRAIL = 0.6  # the rest: 60% below the highest close since entry
ADD_AT = 0.5  # the averaging-down version: a second lot 50% below the first entry


COVER = 0.25  # an hour is in the data if it holds at least a quarter of the median hour's swaps
HOUR_MS = 3_600_000


def gaps(b: pl.DataFrame, grads: pl.DataFrame) -> list[int]:
    """Hours (ms since epoch // HOUR_MS) the swap files miss or only partly hold: fewer
    than COVER x the median hour's swaps across the graduate pools in their windows."""
    t = b.join(grads.select("pid", "grad_ms"), on="pid").select(
        hour=(pl.col("grad_ms") + pl.col("minute") * MINUTE_MS) // HOUR_MS, n="n"
    )
    h = t.group_by("hour").agg(pl.col("n").sum())
    every = pl.DataFrame({"hour": pl.int_range(t["hour"].min(), t["hour"].max() + 1, eager=True)})
    h = every.join(h, on="hour", how="left").with_columns(pl.col("n").fill_null(0))
    cut = COVER * float(h["n"].median())
    return h.filter(pl.col("n") < cut)["hour"].to_list()


def gap_spans(hours: list[int]) -> list[tuple[str, str]]:
    """Runs of consecutive gap hours, as UTC start and end times."""
    spans: list[list[int]] = []
    for hr in sorted(hours):
        if spans and hr == spans[-1][1] + 1:
            spans[-1][1] = hr
        else:
            spans.append([hr, hr])

    def fmt(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, UTC).strftime("%d %b %H:%M")

    return [(fmt(a * HOUR_MS), fmt((z + 1) * HOUR_MS)) for a, z in spans]


def classic_mints(root: Path = pumpswap.SLINKY_ROOT) -> pl.DataFrame:
    from cryptobot.screen import _classic_creates
    from cryptobot.slinky import files

    return pl.concat([_classic_creates(c) for _, c, _ in files(root)]).unique("mint")


def prepare(
    b: pl.DataFrame, grads: pl.DataFrame, classic: pl.DataFrame, gap_hours: list[int] | None = None
) -> pl.DataFrame:
    """Bars of the coins in the test (whole 72 hours in the data: inside its range and
    clear of `gap_hours`), with prices as multiples of the graduation price, the running
    high, the low since that high, and the period."""
    end_ms = (
        b.join(grads, on="pid")["grad_ms"] + b.join(grads, on="pid")["minute"] * MINUTE_MS
    ).max()
    keep = grads.join(classic.select("mint"), on="mint").filter(
        (pl.col("grad_ms") >= FIRST_MS) & (pl.col("grad_ms") + END_MINUTE * MINUTE_MS <= end_ms)
    )
    if gap_hours:
        hit = (
            keep.select("pid", "grad_ms")
            .join(pl.DataFrame({"hour": gap_hours}, schema={"hour": pl.Int64}), how="cross")
            .filter(
                (pl.col("hour") >= pl.col("grad_ms") // HOUR_MS)
                & (pl.col("hour") <= (pl.col("grad_ms") + END_MINUTE * MINUTE_MS) // HOUR_MS)
            )
        )
        keep = keep.join(hit.select("pid").unique(), on="pid", how="anti")
    s0 = pl.col("sol_amount").cast(pl.Float64)
    return (
        b.join(keep.select("pid", "grad_ms", "sol_amount", "k"), on="pid")
        .sort("pid", "minute")
        .with_columns(
            cp=(pl.col("close") / s0) ** 2,
            lp=(pl.col("low") / s0) ** 2,
            hp=(pl.col("high") / s0) ** 2,
            period=pl.when(pl.col("grad_ms") < DISCOVERY_END_MS)
            .then(pl.lit("discovery"))
            .otherwise(pl.lit("holdout")),
        )
        .with_columns(peak=pl.col("hp").cum_max().over("pid"))
        .with_columns(seg=(pl.col("hp") >= pl.col("peak")).cast(pl.Int32).cum_sum().over("pid"))
        .with_columns(trough=pl.col("lp").cum_min().over("pid", "seg"))
        .drop("seg")
    )


def _next_close(x: pl.DataFrame, at: str, out: str) -> pl.DataFrame:
    """For each trade, the first bar after minute `at` (a fill one bar later): its
    minute and reserve. With no later bar, the last bar at or before `at`: the price
    stays where the last trade left it."""
    b = BARS_CACHE["b"].select("pid", "minute")
    pairs = x.select("tid", "pid", at).join(b, on="pid")
    later = (
        pairs.filter(pl.col("minute") > pl.col(at)).group_by("tid").agg(_l=pl.col("minute").min())
    )
    before = (
        pairs.filter(pl.col("minute") <= pl.col(at)).group_by("tid").agg(_b=pl.col("minute").max())
    )
    return (
        x.join(later, on="tid", how="left")
        .join(before, on="tid", how="left")
        .with_columns(pl.coalesce("_l", "_b").alias(f"{out}_m"))
        .drop("_l", "_b")
        .join(
            BARS_CACHE["b"].select(
                "pid", pl.col("minute").alias(f"{out}_m"), pl.col("close").alias(f"{out}_s")
            ),
            on=["pid", f"{out}_m"],
            how="left",
        )
    )


BARS_CACHE: dict = {}


def entries(b: pl.DataFrame, drop: float, rise: float, lo_min: int, hi_min: int) -> pl.DataFrame:
    """The first bottom-bid signal per coin, filled at the next bar."""
    BARS_CACHE["b"] = b
    sig = (
        b.filter(
            (pl.col("minute") >= lo_min)
            & (pl.col("minute") < hi_min)
            & (pl.col("trough") <= pl.col("peak") * (1 - drop))
            & (pl.col("cp") >= pl.col("trough") * (1 + rise))
        )
        .group_by("pid")
        .agg(pl.all().sort_by("minute").first())
        .select("pid", "period", "k", "sol_amount", sig_m="minute", stop_p="trough")
        .with_row_index("tid")
    )
    x = _next_close(sig, "sig_m", "in")
    s_in = pl.col("in_s").cast(pl.Float64)
    return x.with_columns(
        entry_p=(s_in / pl.col("sol_amount")) ** 2,
        tokens=pl.col("k") / s_in - pl.col("k") / (s_in + STAKE * (1 - FEE)),
    )


def _first(x: pl.DataFrame, cond: pl.Expr, name: str, after: str = "in_m") -> pl.DataFrame:
    """Minute of the first bar after `after` meeting `cond` (on the bars' columns)."""
    hit = (
        x.select(
            "tid", "pid", after, *[c for c in ("entry_p", "stop_p", "add_p") if c in x.columns]
        )
        .join(BARS_CACHE["b"].select("pid", "minute", "cp", "lp", "hp"), on="pid")
        .filter((pl.col("minute") > pl.col(after)) & cond)
        .group_by("tid")
        .agg(pl.col("minute").min().alias(name))
    )
    return x.join(hit, on="tid", how="left")


def _sell(x: pl.DataFrame, minute: str, tokens: pl.Expr, out: str) -> pl.DataFrame:
    """Proceeds of selling `tokens` at the bar after `minute` (fee and our impact)."""
    x = _next_close(x.with_columns(pl.col(minute).alias(f"_{out}")), f"_{out}", out)
    s = pl.col(f"{out}_s").cast(pl.Float64)
    t = pl.col("k") / s
    gross = tokens * s / (t + tokens)
    return x.with_columns((gross * (1 - FEE)).alias(f"{out}_lamports"))


def bottom_bid(x: pl.DataFrame, target: float) -> pl.DataFrame:
    """Sell everything at the target, below the low it bought off, or at 72 hours."""
    x = _first(x, pl.col("hp") >= pl.col("entry_p") * target, "tgt_m")
    x = _first(x, pl.col("lp") < pl.col("stop_p"), "stop_m")
    end = pl.lit(END_MINUTE)
    x = x.with_columns(
        exit_m=pl.min_horizontal(
            pl.col("tgt_m").fill_null(END_MINUTE), pl.col("stop_m").fill_null(END_MINUTE), end
        ),
    ).with_columns(  # a bar that shows both: the stop, to be safe
        reason=pl.when(pl.col("stop_m") == pl.col("exit_m"))
        .then(pl.lit("stop"))
        .when(pl.col("tgt_m") == pl.col("exit_m"))
        .then(pl.lit("target"))
        .otherwise(pl.lit("time")),
    )
    x = x.with_columns(
        exit_m=pl.when(pl.col("reason") == "time")
        .then(
            # sell at the last bar in the window, not one after it
            pl.col("exit_m") - 1
        )
        .otherwise(pl.col("exit_m"))
    )
    x = _sell(x, "exit_m", pl.col("tokens"), "out")
    return x.with_columns(net_return=pl.col("out_lamports") / STAKE - 1)


def conviction(x: pl.DataFrame, add: bool) -> pl.DataFrame:
    """A third at 2x, a third at 4x, the rest 60% below the highest close since entry or
    at 72 hours; with `add`, a second lot 50% below the first entry before any sale."""
    x = x.with_columns(add_p=pl.col("entry_p") * ADD_AT)
    for i, (mult, _) in enumerate(STAGES):
        x = _first(x, pl.col("hp") >= pl.col("entry_p") * mult, f"st{i}_m")
    # trailing: the running max close since entry (the entry bar's close included)
    trail = (
        x.select("tid", "pid", "in_m")
        .join(BARS_CACHE["b"].select("pid", "minute", "cp"), on="pid")
        .filter(pl.col("minute") >= pl.col("in_m"))
        .sort("tid", "minute")
        .with_columns(top=pl.col("cp").cum_max().over("tid"))
        .filter((pl.col("minute") > pl.col("in_m")) & (pl.col("cp") <= pl.col("top") * (1 - TRAIL)))
        .group_by("tid")
        .agg(trail_m=pl.col("minute").min())
    )
    x = x.join(trail, on="tid", how="left").with_columns(
        last_m=pl.min_horizontal(pl.col("trail_m").fill_null(END_MINUTE), pl.lit(END_MINUTE))
    )
    lot2 = pl.lit(0.0)
    cost = pl.lit(float(STAKE))
    if add:
        x = _first(x, pl.col("cp") <= pl.col("add_p"), "add_m")
        first_sale = pl.min_horizontal(pl.col("st0_m").fill_null(END_MINUTE), pl.col("last_m"))
        x = x.with_columns(add_m=pl.when(pl.col("add_m") < first_sale).then(pl.col("add_m")))
        # the second lot fills at the bar after add_m (lot2_m); add_m stays the decision
        x = _next_close(x.with_columns(_add=pl.col("add_m").fill_null(-1)), "_add", "lot2")
        s_add = pl.col("lot2_s").cast(pl.Float64)
        x = x.with_columns(
            tokens2=pl.when(pl.col("add_m").is_not_null())
            .then(pl.col("k") / s_add - pl.col("k") / (s_add + STAKE * (1 - FEE)))
            .otherwise(0.0)
        )
        lot2 = pl.col("tokens2")
        cost = pl.when(pl.col("add_m").is_not_null()).then(2.0 * STAKE).otherwise(float(STAKE))
    held = pl.col("tokens") + lot2
    proceeds = pl.lit(0.0)
    sold = pl.lit(0.0)
    for i, (_, share) in enumerate(STAGES):
        m = pl.col(f"st{i}_m")
        x = x.with_columns(**{f"_st{i}": pl.when(m < pl.col("last_m")).then(m)})
        x = _sell(
            x.with_columns(pl.col(f"_st{i}").fill_null(END_MINUTE)),
            f"_st{i}",
            held * share,
            f"s{i}",
        )
        part = pl.when(pl.col(f"_st{i}") < pl.col("last_m")).then(1.0).otherwise(0.0)
        proceeds = proceeds + pl.col(f"s{i}_lamports") * part
        sold = sold + share * part
    x = x.with_columns(
        _last=pl.when(pl.col("trail_m").is_not_null() & (pl.col("trail_m") < END_MINUTE))
        .then(pl.col("last_m"))
        .otherwise(END_MINUTE - 1)
    )
    x = _sell(x, "_last", held * (1 - sold), "rest")
    reason = pl.when(pl.col("trail_m").is_not_null() & (pl.col("trail_m") < END_MINUTE))
    return x.with_columns(
        net_return=(proceeds + pl.col("rest_lamports")) / cost - 1,
        reason=reason.then(pl.lit("trail")).otherwise(pl.lit("time")),
    )


def cells(b: pl.DataFrame) -> pl.DataFrame:
    """Every pre-registered cell's trades: entry, exit set, period, return, reason."""
    out = []
    for drop in DROPS:
        for rise in RISES:
            for age, lo, hi in AGES:
                x = entries(b, drop, rise, lo, hi)
                if x.is_empty():
                    continue
                entry = f"drop{round(drop * 100)}|rise{round(rise * 100)}|{age}"
                runs = [(f"bottom tp{t:g}x", bottom_bid(x, t)) for t in TARGETS]
                runs += [
                    ("conviction", conviction(x, False)),
                    ("conviction+add", conviction(x, True)),
                ]
                for exit_name, r in runs:
                    out.append(
                        r.select("pid", "period", "net_return", "reason").with_columns(
                            entry=pl.lit(entry), exit=pl.lit(exit_name)
                        )
                    )
    return pl.concat(out)


def base_rate(b: pl.DataFrame) -> pl.DataFrame:
    """Every coin bought one hour after graduation and held to 72 hours."""
    sig = (
        b.filter(pl.col("minute") >= 60)
        .group_by("pid")
        .agg(pl.all().sort_by("minute").first())
        .select("pid", "period", "k", "sol_amount", sig_m="minute")
        .with_row_index("tid")
    )
    BARS_CACHE["b"] = b
    x = _next_close(sig, "sig_m", "in")
    s_in = pl.col("in_s").cast(pl.Float64)
    x = x.with_columns(
        tokens=pl.col("k") / s_in - pl.col("k") / (s_in + STAKE * (1 - FEE)),
        exit_m=pl.lit(END_MINUTE - 1),
    )
    x = _sell(x, "exit_m", pl.col("tokens"), "out")
    return x.select("pid", "period", net_return=pl.col("out_lamports") / STAKE - 1)


def closes_only(t: pl.DataFrame) -> pl.DataFrame:
    """The same bars with each bar's low and high set to its close, and the running high
    and low rebuilt: a check that wicks inside a minute (or a badly solved swap) don't
    decide the result."""
    return (
        t.with_columns(lp=pl.col("cp"), hp=pl.col("cp"))
        .with_columns(peak=pl.col("hp").cum_max().over("pid"))
        .with_columns(seg=(pl.col("hp") >= pl.col("peak")).cast(pl.Int32).cum_sum().over("pid"))
        .with_columns(trough=pl.col("lp").cum_min().over("pid", "seg"))
        .drop("seg")
    )


def wicks(t: pl.DataFrame) -> tuple[float, float]:
    """Share of bars whose low is under half the lowest of its own and its neighbours'
    closes, and whose high is over twice the highest."""
    x = t.with_columns(
        pc=pl.col("cp").shift(1).over("pid"), nc=pl.col("cp").shift(-1).over("pid")
    ).drop_nulls(["pc", "nc"])
    lo = (x["lp"] < 0.5 * x.select(pl.min_horizontal("pc", "nc", "cp")).to_series()).mean()
    hi = (x["hp"] > 2.0 * x.select(pl.max_horizontal("pc", "nc", "cp")).to_series()).mean()
    return float(lo), float(hi)


def cross_check(b: pl.DataFrame, grads: pl.DataFrame, n: int = 300) -> tuple[int, float] | None:
    """Bar closes against pumpswap.pool_path on the first 65 minutes of `n` random pools
    (pumpswap.py's slices, where present): bars compared and the share off by over 1%."""
    if not any(pumpswap.OUT.glob("swaps-*.parquet")):
        return None
    pick = grads.join(b.select("pid").unique(), on="pid").sample(min(n, grads.height), seed=1)
    paths = pumpswap.load(pick["mint"].to_list())
    off = total = 0
    for pid, mint, grad_ms in pick.select("pid", "mint", "grad_ms").iter_rows():
        _, _, ts, sol, _ = paths[mint]
        if len(ts) == 0:
            continue
        mine = b.filter((pl.col("pid") == pid) & (pl.col("minute") < 65))
        for minute, close in mine.select("minute", "close").iter_rows():
            i = int(np.searchsorted(ts, grad_ms + (minute + 1) * MINUTE_MS, side="left"))
            if i:
                total += 1
                off += abs(close / sol[i - 1] - 1) > 0.01
    return total, off / total if total else 0.0


def summary(df: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    z = 1.959964
    return df.group_by(by).agg(
        n=pl.len(),
        mean=pl.col("net_return").mean(),
        median=pl.col("net_return").median(),
        win=(pl.col("net_return") > 0).mean(),
        half=pl.when(pl.len() >= 2).then(z * pl.col("net_return").std() / pl.len().sqrt()),
    )


def report(b: pl.DataFrame, grads: pl.DataFrame) -> str:
    from cryptobot.screen import _ci, _pct

    classic = classic_mints()
    holes = gaps(b, grads)
    whole = prepare(b, grads, classic)
    t = prepare(b, grads, classic, holes)
    df = cells(t)
    base = base_rate(t)
    s = summary(df, ["entry", "exit", "period"])
    disc = s.filter((pl.col("period") == "discovery") & (pl.col("n") >= 200)).sort(
        "mean", descending=True
    )
    hold = s.filter(pl.col("period") == "holdout").select(
        "entry", "exit", n_h="n", mean_h="mean", half_h="half"
    )
    joined = disc.join(hold, on=["entry", "exit"], how="left")
    coins = t.select("pid", "period").unique().group_by("period").len().sort("period")
    lines = [
        "# After graduation: V4's bottom bids and conviction holds",
        "",
        "Generated by `uv run python -m cryptobot.migrated report`, the pre-registered test "
        "in `docs/research/2026-10-28-preregistered-after-graduation-test.md`. Classic "
        "Pump.fun graduates of 1 Sep-8 Oct 2026 with 72 hours of PumpSwap trading in the "
        "data (Slinky21 `swap`, one-minute bars); 0.25 SOL, 1.25% pool fee a side, fills one "
        "bar after each decision with our own price impact; no network costs.",
        "",
        "Coins: " + ", ".join(f"{p} {n:,}" for p, n in coins.rows()) + ".",
        "",
        f"Data gaps (hours with under {COVER:.0%} of the median hour's swaps; amendment of 28 "
        "Oct): " + ("; ".join(f"{a} to {z}" for a, z in gap_spans(holes)) or "none") + ". "
        f"Coins whose 72 hours touch one are left out: {whole['pid'].n_unique():,} graduates "
        f"had the window inside the data's range, {t['pid'].n_unique():,} remain.",
        "",
    ]
    bs = summary(base, ["period"]).sort("period")
    lines += [
        "## Base rate: buy one hour after graduation, hold 72 hours",
        "",
        "| Period | Trades | Mean [95% CI] | Median | Win rate |",
        "|---|---|---|---|---|",
    ]
    for r in bs.iter_rows(named=True):
        lines.append(
            f"| {r['period']} | {r['n']:,} | {_ci(r['mean'], r['half'])} | "
            f"{_pct(r['median'])} | {_pct(r['win'])} |"
        )
    lines += [
        "",
        "## Every cell (discovery 1-23 Sep, at least 200 trades; holdout from 24 Sep)",
        "",
        "| Rank | Entry | Exit | Disc. n | Disc. mean [95% CI] | Median | Win | "
        "Hold. n | Hold. mean [95% CI] |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(joined.iter_rows(named=True), 1):
        lines.append(
            f"| {i} | {r['entry']} | {r['exit']} | {r['n']:,} | {_ci(r['mean'], r['half'])} | "
            f"{_pct(r['median'])} | {_pct(r['win'])} | {r['n_h'] or 0:,} | "
            f"{_ci(r['mean_h'], r['half_h'])} |"
        )
    if joined.height:
        best = joined.row(0, named=True)
        ok = (
            best["half_h"] is not None
            and best["mean_h"] - best["half_h"] > 0
            and best["mean_h"] > 0
        )
        lines += [
            "",
            f"**Pre-set test:** best discovery cell `{best['entry']} {best['exit']}` -> "
            + ("**pass**" if ok else "**fail**")
            + f" (holdout mean {_ci(best['mean_h'], best['half_h'])}, n {best['n_h'] or 0:,}).",
        ]
        reasons = (
            df.filter((pl.col("entry") == best["entry"]) & (pl.col("exit") == best["exit"]))
            .group_by("period", "reason")
            .len()
            .sort("period", "reason")
        )
        lines += [
            "",
            "How its trades ended: "
            + "; ".join(f"{p} {why} {n:,}" for p, why, n in reasons.rows()),
        ]
    pos = joined.filter(pl.col("mean") > 0)
    both = pos.filter(pl.col("mean_h") > 0)
    lines += [
        "",
        f"Cells positive on discovery: {pos.height} of {joined.height}; of those, "
        f"positive on the holdout too: {both.height}.",
        "",
        "## Checks (not part of the pre-registered rule)",
        "",
    ]
    check = cross_check(b, grads)
    if check is not None:
        lines.append(
            f"- **Bars against `pumpswap.pool_path`:** {check[0]:,} one-minute closes in the "
            f"first 65 minutes of 300 random pools; {_pct(check[1])} differ by over 1%."
        )
    lo, hi = wicks(t)
    c = summary(cells(closes_only(t)), ["entry", "exit", "period"])
    best = c.group_by("period").agg(pl.col("mean").max()).sort("period")
    n_pos = c.filter(pl.col("mean") > 0).height
    lines += [
        f"- **Wicks:** {lo:.3%} of the test coins' bars have a low under half the lowest "
        f"nearby close, {hi:.3%} a high over twice the highest. Stops, targets and the "
        "high and low the entry measures use those, so every cell was rerun on closes only: "
        "best mean "
        + ", ".join(f"{p} {_pct(v)}" for p, v in best.rows())
        + f"; cells with a positive mean in either period: {n_pos} of {c.height}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("extract", "report"))
    parser.add_argument("--from", dest="start", default="2026-09-01")
    args = parser.parse_args(argv)
    if args.command == "extract":
        extract(args.start)
    else:
        print(report(*load()))


if __name__ == "__main__":
    main()
