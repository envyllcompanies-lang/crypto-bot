"""Tracked wallets: traders who keep making money across many tokens.

V1 avoids tokens with "a ton of tracked wallets" in them (Terminal shows wallets its
users follow). We have no such list, so we build one from recorded trades:

- Positions are per wallet and token, with fees included: buys cost SOL plus protocol
  and creator fees, sells return SOL minus them. A position counts once the wallet has
  sold at least `closed_share` of what it bought.
- Bots and snipers are dropped: wallets that bought most tokens in the launch slot
  or the next one, that hold for only seconds, that close implausibly many positions
  an hour, or that created tokens themselves.
- A wallet is tracked when it closed enough positions, won on enough distinct tokens,
  wins at least half the time and is up overall.
- A holdout check scores wallets on the earlier part of the data and tests the list on
  the later part, so the report shows whether the list finds skill or just past luck.

Usage:
    uv run python -m cryptobot.tracked            # build from the server's recordings
    uv run python -m cryptobot.tracked --source slinky   # from the M1 trade sample

The list is written to data/derived/tracked_wallets.parquet. The paper trader loads it
and counts listed wallets among each candidate's holders.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from cryptobot.curve import INITIAL_VIRTUAL_SOL, LAMPORTS_PER_SOL
from cryptobot.fetch import RAW
from cryptobot.recorder import LIVE

DERIVED = RAW.parent / "derived"
TRACKED_PATH = DERIVED / "tracked_wallets.parquet"
META_PATH = DERIVED / "tracked_wallets.json"
PAPER = LIVE.parent / "paper"
SLINKY = RAW / "Slinky21__Pumpfun_v2_dataset"

TRADE_COLS = [
    "mint",
    "user",
    "is_buy",
    "sol_amount",
    "token_amount",
    "fee",
    "creator_fee",
    "slot",
    "ts_ms",
]
CREATE_COLS = ["mint", "creator", "slot"]


@dataclass(frozen=True)
class Params:
    closed_share: float = 0.9  # sold at least this share of the tokens bought
    min_closed: int = 5  # closed positions on distinct tokens
    min_winning_tokens: int = 3
    min_win_rate: float = 0.5
    min_pnl_lamports: int = LAMPORTS_PER_SOL // 2  # up at least 0.5 SOL overall
    max_sniper_share: float = 0.5  # share of positions opened in the launch slot or next
    min_median_hold_ms: int = 5_000
    max_positions_per_hour: float = 60.0
    launch_slots: int = 1
    holdout_share: float = 0.3  # last share of the time span used to test the list


DEFAULT = Params()


def _classic(creates: pl.DataFrame) -> pl.DataFrame:
    """SOL-quoted launches outside Mayhem mode, as (mint, creator, slot)."""
    df = creates
    if "virtual_sol_reserves" in df.columns:
        df = df.filter(pl.col("virtual_sol_reserves") == INITIAL_VIRTUAL_SOL)
    if "is_mayhem_mode" in df.columns:
        df = df.filter(~pl.col("is_mayhem_mode").fill_null(False))
    return df.select(pl.col("mint"), pl.col("creator"), pl.col("slot").cast(pl.Int64)).unique(
        "mint", keep="first"
    )


Trades = pl.DataFrame | pl.LazyFrame


def positions(trades: Trades, creates: pl.DataFrame, p: Params = DEFAULT) -> pl.DataFrame:
    """One row per (wallet, token) on classic launches, with fees in cost and proceeds.

    `trades` may be lazy: the aggregation streams, so days of recordings fit in the
    server's memory.
    """
    launches = _classic(creates).rename({"slot": "create_slot"})
    t = trades.lazy().join(launches.select("mint", "create_slot").lazy(), on="mint", how="inner")
    fees = pl.col("fee").fill_null(0) + pl.col("creator_fee").fill_null(0)
    buy = pl.col("is_buy")
    return (
        t.group_by("user", "mint")
        .agg(
            bought=pl.col("token_amount").filter(buy).sum(),
            sold=pl.col("token_amount").filter(~buy).sum(),
            cost=(pl.col("sol_amount") + fees).filter(buy).sum(),
            proceeds=(pl.col("sol_amount") - fees).filter(~buy).sum(),
            first_buy_ms=pl.col("ts_ms").filter(buy).min(),
            last_sell_ms=pl.col("ts_ms").filter(~buy).max(),
            first_buy_slot=pl.col("slot").filter(buy).min(),
            create_slot=pl.col("create_slot").first(),
        )
        .filter(pl.col("bought") > 0)
        .with_columns(
            closed=pl.col("sold") >= pl.col("bought") * p.closed_share,
            sniped=pl.col("first_buy_slot") <= pl.col("create_slot") + p.launch_slots,
        )
        .with_columns(
            pnl=pl.col("proceeds") - pl.col("cost"),
            hold_ms=pl.col("last_sell_ms") - pl.col("first_buy_ms"),
        )
        .collect(streaming=True)
    )


def score(pos: pl.DataFrame, creates: pl.DataFrame, p: Params = DEFAULT) -> pl.DataFrame:
    """Per wallet: activity, results, bot flags and whether it is tracked."""
    creators = set(creates["creator"].to_list())
    closed = pos.filter("closed")
    per = (
        pos.group_by("user")
        .agg(
            positions=pl.len(),
            sniper_share=pl.col("sniped").mean(),
            first_ms=pl.col("first_buy_ms").min(),
            last_ms=pl.max_horizontal("first_buy_ms", "last_sell_ms").max(),
        )
        .join(
            closed.group_by("user").agg(
                closed=pl.len(),
                wins=(pl.col("pnl") > 0).sum(),
                pnl=pl.col("pnl").sum(),
                cost=pl.col("cost").sum(),
                median_hold_ms=pl.col("hold_ms").median(),
            ),
            on="user",
            how="left",
        )
        .with_columns(
            pl.col("closed", "wins").fill_null(0),
            pl.col("pnl", "cost").fill_null(0),
        )
    )
    hours = ((pl.col("last_ms") - pl.col("first_ms")) / 3_600_000).clip(lower_bound=1.0)
    per = per.with_columns(
        win_rate=pl.when(pl.col("closed") > 0).then(pl.col("wins") / pl.col("closed")),
        positions_per_hour=pl.col("closed") / hours,
        creator=pl.col("user").is_in(list(creators)),
    )
    bot = (
        (pl.col("sniper_share") >= p.max_sniper_share)
        | (pl.col("median_hold_ms") < p.min_median_hold_ms)
        | (pl.col("positions_per_hour") > p.max_positions_per_hour)
        | pl.col("creator")
    )
    good = (
        (pl.col("closed") >= p.min_closed)
        & (pl.col("wins") >= p.min_winning_tokens)
        & (pl.col("win_rate") >= p.min_win_rate)
        & (pl.col("pnl") >= p.min_pnl_lamports)
    )
    return per.with_columns(bot=bot.fill_null(False)).with_columns(
        tracked=(good & ~pl.col("bot")).fill_null(False)
    )


def holdout(trades: Trades, creates: pl.DataFrame, p: Params = DEFAULT) -> dict:
    """Build the list on the earlier data, then compare listed wallets on the later data.

    Positions are rebuilt separately on each side of the split, so a position opened
    before the split and closed after it is not counted on either side.
    """
    lo, hi = (
        trades.lazy().select(lo=pl.col("ts_ms").min(), hi=pl.col("ts_ms").max()).collect().row(0)
    )
    if lo is None or hi is None or hi <= lo:
        return {"usable": False}
    cut = int(hi - (hi - lo) * p.holdout_share)
    train = score(positions(trades.filter(pl.col("ts_ms") < cut), creates, p), creates, p)
    listed = set(train.filter("tracked")["user"].to_list())
    test = positions(trades.filter(pl.col("ts_ms") >= cut), creates, p).filter("closed")
    bots = set(train.filter("bot")["user"].to_list())
    test = test.filter(~pl.col("user").is_in(list(bots)))
    groups = (
        test.with_columns(group=pl.col("user").is_in(list(listed)))
        .group_by("group")
        .agg(
            positions=pl.len(),
            wallets=pl.col("user").n_unique(),
            win_rate=(pl.col("pnl") > 0).mean(),
            return_on_cost=pl.col("pnl").sum() / pl.col("cost").sum(),
        )
    )
    out = {"usable": True, "split_ms": cut, "listed_in_train": len(listed)}
    for row in groups.iter_rows(named=True):
        out["listed" if row["group"] else "others"] = {
            k: row[k] for k in ("positions", "wallets", "win_rate", "return_on_cost")
        }
    return out


# Loading ----------------------------------------------------------------------


def _read(files: list[Path]) -> pl.DataFrame | None:
    files = [f for f in files if f.exists()]
    if not files:
        return None
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def _recent(roots: list[Path], table: str, days: int) -> list[Path]:
    """Files of one table (layout <root>/<table>/<date>/) in the date folders that can
    hold the last `days` days before the newest data, across all roots: the newest date
    and the `days` dates before it. Folders are named by the UTC date they were written,
    so the newest is usually a partial day (at 00:10 UTC, ten minutes); the caller cuts
    rows to the exact window. Picking per root instead would let a run that ended weeks
    ago pass its last day off as recent."""
    folders = [d for r in roots for d in (r / table).glob("20??-??-??") if d.is_dir()]
    if not folders:
        return []
    newest = max(date.fromisoformat(d.name) for d in folders)
    first = newest - timedelta(days=days)
    keep = sorted((d for d in folders if date.fromisoformat(d.name) >= first), key=str)
    return [f for d in keep for f in sorted(d.glob("*.parquet"))]


def load_recorded(
    roots: list[Path] | None = None, days: int = 1
) -> tuple[pl.LazyFrame, pl.DataFrame]:
    """Recent trades (lazy) and creates from the recorder folder and every paper run."""
    if roots is None:
        roots = [LIVE, *sorted(PAPER.glob("*/events"))]
    trade_files = _recent(roots, "trade", days)
    creates = _read(_recent(roots, "create", days + 1))
    if not trade_files or creates is None:
        raise FileNotFoundError("no recorded trade and create events found")
    cols = [*TRADE_COLS[:-1], "received_ms", "signature", "event_index"]
    trades = (
        pl.concat([pl.scan_parquet(f).select(cols) for f in trade_files], how="diagonal_relaxed")
        .unique(["signature", "event_index"])
        .rename({"received_ms": "ts_ms"})
        .select(TRADE_COLS)
    )
    newest = trades.select(pl.col("ts_ms").max()).collect().item()
    if newest is not None:  # exactly `days` x 24 hours, ending at the newest trade
        trades = trades.filter(pl.col("ts_ms") >= newest - days * 86_400_000)
    return trades, creates.unique("mint")


def load_slinky(root: Path = SLINKY) -> tuple[pl.LazyFrame, pl.DataFrame]:
    """The M1 trade sample (lazy) in the same shape as recorded events."""
    files = sorted(root.rglob("trade_*.parquet"))
    creates = _read(sorted(root.rglob("create_*.parquet")))
    if not files or creates is None:
        raise FileNotFoundError(f"no Slinky21 sample under {root}")
    cols = [
        "mint",
        "trade_user",
        "ix_name",
        "sol_amount",
        "token_amount",
        "fee",
        "creator_fee",
        "slot",
        "block_time",
    ]
    trades = (
        pl.scan_parquet(files)
        .select(cols)
        .with_columns(
            user=pl.col("trade_user"),
            is_buy=pl.col("ix_name") != "sell",
            ts_ms=pl.col("block_time").dt.epoch("ms"),
        )
    )
    return trades.select(TRADE_COLS), creates


def save(scored: pl.DataFrame, meta: dict, path: Path = TRACKED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    scored.filter("tracked").sort("pnl", descending=True).write_parquet(tmp)
    tmp.rename(path)
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1))


def load_top(path: Path = TRACKED_PATH, n: int = 50) -> list[str] | None:
    """The `n` most profitable tracked wallets (the file is sorted by profit)."""
    if not path.exists():
        return None
    return pl.read_parquet(path, columns=["user"])["user"].head(n).to_list()


def load_list(path: Path = TRACKED_PATH) -> frozenset[str] | None:
    """The saved tracked-wallet list, or None if none has been built yet."""
    if not path.exists():
        return None
    return frozenset(pl.read_parquet(path, columns=["user"])["user"].to_list())


def report(scored: pl.DataFrame, meta: dict) -> str:
    sol = LAMPORTS_PER_SOL
    tracked = scored.filter("tracked")
    lines = [
        "# Tracked wallets",
        "",
        f"Built from {meta['trades']:,} trades on {meta['tokens']:,} classic tokens "
        f"({meta['source']}).",
        "",
        "| | Wallets |",
        "|---|---|",
        f"| With any position | {scored.height:,} |",
        f"| Dropped as bots, snipers or creators | {scored['bot'].sum():,} |",
        f"| Tracked | {tracked.height:,} |",
        "",
    ]
    h = meta["holdout"]
    if h.get("usable") and "listed" in h and "others" in h:
        lines += [
            "Holdout: list built on the earlier part, tested on the last "
            f"{meta['params']['holdout_share']:.0%} of the time span.",
            "",
            "| Group | Wallets | Closed positions | Win rate | Return on cost |",
            "|---|---|---|---|---|",
        ]
        for g in ("listed", "others"):
            r = h[g]
            lines.append(
                f"| {g} | {r['wallets']:,} | {r['positions']:,} | {r['win_rate']:.1%} "
                f"| {r['return_on_cost']:+.1%} |"
            )
        lines.append("")
    else:
        lines += ["Holdout: not enough data to test the list yet.", ""]
    if tracked.height:
        lines += [
            "Top 10 by profit:",
            "",
            "| Wallet | Closed | Win rate | Profit (SOL) |",
            "|---|---|---|---|",
        ]
        for r in tracked.sort("pnl", descending=True).head(10).iter_rows(named=True):
            lines.append(
                f"| {r['user'][:6]}…{r['user'][-4:]} | {r['closed']} | {r['win_rate']:.0%} "
                f"| {r['pnl'] / sol:+.2f} |"
            )
    return "\n".join(lines)


def build(source: str, p: Params = DEFAULT, days: int = 1) -> tuple[pl.DataFrame, dict]:
    if source == "slinky":
        trades, creates = load_slinky()
    else:
        trades, creates = load_recorded(days=days)
    pos = positions(trades, creates, p)
    scored = score(pos, creates, p)
    span = (
        trades.lazy()
        .select(lo=pl.col("ts_ms").min(), hi=pl.col("ts_ms").max(), n=pl.len())
        .collect(streaming=True)
        .row(0, named=True)
    )
    meta = {
        "source": source,
        "built_from_ms": int(span["lo"]),
        "built_to_ms": int(span["hi"]),
        "trades": int(span["n"]),
        "tokens": pos["mint"].n_unique(),
        "tracked": int(scored["tracked"].sum()),
        "params": asdict(p),
        "holdout": holdout(trades, creates, p),
    }
    return scored, meta


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", choices=("recorded", "slinky"), default="recorded")
    parser.add_argument("--out", type=Path, default=TRACKED_PATH)
    parser.add_argument("--days", type=int, default=1, help="recorded days to use (default 1)")
    args = parser.parse_args(argv)
    try:
        scored, meta = build(args.source, days=args.days)
    except FileNotFoundError as e:
        print(f"tracked wallets: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    save(scored, meta, args.out)
    print(report(scored, meta))


if __name__ == "__main__":
    main()
