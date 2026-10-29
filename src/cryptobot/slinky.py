"""Slinky21/Pumpfun_v2_dataset as a stream of recorder-shaped events, for backtests.

The dataset holds decoded Pump.fun events in 12-hour Parquet files (see data/README.md).
This module turns its `create` and `trade` tables into the rows our live recorder writes
(recorder.SCHEMAS), so the paper engine can replay them exactly as it trades live.

- Order: true chain order (slot, then position in the block; a create before the trades
  of the same transaction). That keeps the reserve chain intact.
- Time: `received_ms` is when the dataset's own recorder ingested the event, made
  non-decreasing in chain order. That is the same meaning our recorder gives it, and
  it carries realistic feed delay, unlike the 1-second block time.
- Memory: trade files are read one row group (~50,000 rows) at a time, so a two-week
  replay fits beside the paper trader on a 1 GB server.
- Curves quoted in other assets have zero SOL reserves on every trade; they are dropped
  here, as the engine would ignore them anyway.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from cryptobot.fetch import local_dir

DATASET = "Slinky21/Pumpfun_v2_dataset"
SLINKY_ROOT = local_dir(DATASET)
FILE_SPAN = timedelta(hours=13)  # a 12-hour file, with slack
STAMP = re.compile(r"_(\d{8}T\d{6})\.parquet$")

CREATE_COLS = {
    "mint": "mint",
    "creator": "creator",
    "tx_signer": "user",
    "token_name": "name",
    "symbol": "symbol",
    "uri": "uri",
    "bonding_curve": "bonding_curve",
    "token_program": "token_program",
    "is_mayhem_mode": "is_mayhem_mode",
    "token_total_supply": "token_total_supply",
    "real_token_reserves": "real_token_reserves",
    "virtual_sol_reserves": "virtual_sol_reserves",
    "virtual_token_reserves": "virtual_token_reserves",
    "signature": "signature",
}
# Only what the engine reads (features.TokenState.apply_trade, paper.Engine): the long
# strings it never uses (signature, creator, fee recipient) would triple the memory.
TRADE_COLS = {
    "mint": "mint",
    "trade_user": "user",
    "ix_name": "ix_name",
    "sol_amount": "sol_amount",
    "token_amount": "token_amount",
    "virtual_sol_reserves": "virtual_sol_reserves",
    "virtual_token_reserves": "virtual_token_reserves",
}


def files(root: Path = SLINKY_ROOT) -> list[tuple[datetime, Path, Path]]:
    """(file stamp, create file, trade file) for each 12-hour chunk with both tables."""
    creates = {m.group(1): p for p in root.rglob("create_*.parquet") if (m := STAMP.search(p.name))}
    out = []
    for p in root.rglob("trade_*.parquet"):
        m = STAMP.search(p.name)
        if m and m.group(1) in creates:
            stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            out.append((stamp, creates[m.group(1)], p))
    return sorted(out)


def _common(lf: pl.LazyFrame) -> pl.LazyFrame:
    return lf.with_columns(
        slot=pl.col("slot").cast(pl.UInt64),
        tx_index=pl.col("tx_index").cast(pl.Int64),
        timestamp=pl.col("block_time").dt.epoch("s"),
        ingested_ms=pl.col("ingested_at").dt.epoch("ms"),
        block_ms=pl.col("block_time").dt.epoch("ms"),
    )


def _creates(lf: pl.LazyFrame) -> pl.LazyFrame:
    lf = lf.select(*CREATE_COLS, "slot", "tx_index", "block_time", "ingested_at")
    return _common(lf.rename(CREATE_COLS)).with_columns(_kind=pl.lit("create"), _order=0)


def _trades(lf: pl.LazyFrame) -> pl.LazyFrame:
    lf = lf.select(*TRADE_COLS, "slot", "tx_index", "block_time", "ingested_at")
    lf = lf.rename(TRADE_COLS).filter(pl.col("virtual_sol_reserves") > 0)
    return (
        _common(lf)
        .with_columns(is_buy=pl.col("ix_name") != "sell", _kind=pl.lit("trade"), _order=1)
        .drop("ix_name")
    )


def _ms(value: object) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return None


def _batches(
    create_path: Path, trade_path: Path, start: datetime | None, end: datetime | None
) -> Iterator[pl.DataFrame]:
    """One 12-hour chunk as batches sorted in chain order, a row group at a time.

    The trade file is stored in time-ordered row groups of ~50,000 rows. After reading
    one, every event older than the earliest block time still to come (from the file's
    row-group statistics) is complete and goes out; the rest waits for the next group.
    Creates (a small table) are merged in by the same cut-off.
    """

    def keep(lf: pl.LazyFrame) -> pl.LazyFrame:
        if start is not None:
            lf = lf.filter(pl.col("block_time") >= start)
        if end is not None:
            lf = lf.filter(pl.col("block_time") < end)
        return lf

    creates = keep(_creates(pl.scan_parquet(create_path))).collect().sort("block_ms")
    created = creates["block_ms"]
    pf = pq.ParquetFile(trade_path)
    meta = pf.metadata
    col = pf.schema_arrow.names.index("block_time")
    mins: list[int | None] = []
    maxs: list[int | None] = []
    for i in range(meta.num_row_groups):
        stats = meta.row_group(i).column(col).statistics
        known = stats is not None and stats.has_min_max
        mins.append(_ms(stats.min) if known else None)
        maxs.append(_ms(stats.max) if known else None)
    # later[i]: the earliest block time in any row group after i, or None if one of them
    # has no statistics (then hold everything until the end) or i is the last.
    later: list[int | None] = [None] * meta.num_row_groups
    low: int | None = None
    unknown = False
    for i in range(meta.num_row_groups - 1, -1, -1):
        later[i] = None if unknown else low
        if mins[i] is None:
            unknown = True
        else:
            low = mins[i] if low is None else min(low, mins[i])
    lo_ms = None if start is None else _ms(start)
    hi_ms = None if end is None else _ms(end)
    columns = [*TRADE_COLS, "slot", "tx_index", "block_time", "ingested_at"]
    pending: pl.DataFrame | None = None
    taken = 0
    for i in range(meta.num_row_groups):
        cutoff = later[i]
        outside = (lo_ms is not None and maxs[i] is not None and maxs[i] < lo_ms) or (
            hi_ms is not None and mins[i] is not None and mins[i] >= hi_ms
        )
        if outside:
            rows = pl.DataFrame()
        else:
            group = pl.from_arrow(pf.read_row_group(i, columns=columns))
            assert isinstance(group, pl.DataFrame)
            rows = keep(_trades(group.lazy())).collect()
        if rows.height:
            pending = rows if pending is None else pl.concat([pending, rows])
        if cutoff is None and i < meta.num_row_groups - 1:
            continue  # unknown boundary: keep collecting
        ready = None
        if cutoff is None:
            ready, pending, upto = pending, None, creates.height
        else:
            if pending is not None:
                ready = pending.filter(pl.col("block_ms") < cutoff)
                pending = pending.filter(pl.col("block_ms") >= cutoff)
            upto = int((created < cutoff).sum())
        parts = [creates.slice(taken, upto - taken)] + ([ready] if ready is not None else [])
        batch = pl.concat(parts, how="diagonal_relaxed")
        taken = upto
        if batch.height:
            yield batch.sort(["slot", "tx_index", "_order"], maintain_order=True)
    if taken < creates.height:  # a trade file with no row groups
        yield creates.slice(taken).sort(["slot", "tx_index", "_order"], maintain_order=True)


def events(
    root: Path = SLINKY_ROOT, start: datetime | None = None, end: datetime | None = None
) -> Iterator[tuple[str, dict]]:
    """("create" | "trade", row) in chain order, for block times in [start, end)."""
    last_ms = 0
    seq = 0
    for stamp, create_path, trade_path in files(root):
        # A file holds the 12 hours (and a little more) before its stamp.
        if start is not None and stamp < start:
            continue
        if end is not None and stamp - FILE_SPAN > end:
            continue
        for batch in _batches(create_path, trade_path, start, end):
            group: list[dict] = []
            for row in batch.iter_rows(named=True):
                if group and (row["slot"], row["tx_index"]) != (
                    group[0]["slot"],
                    group[0]["tx_index"],
                ):
                    for r in chain_order(group):
                        last_ms = max(last_ms, r.pop("ingested_ms"))
                        r["received_ms"], r["seq"], r["event_index"] = last_ms, seq, 0
                        seq += 1
                        yield _clean(r)
                    group = []
                group.append(row)
            for r in chain_order(group):
                last_ms = max(last_ms, r.pop("ingested_ms"))
                r["received_ms"], r["seq"], r["event_index"] = last_ms, seq, 0
                seq += 1
                yield _clean(r)


def _clean(row: dict) -> tuple[str, dict]:
    kind = row.pop("_kind")
    for k in ("_order", "block_ms", "block_time", "ingested_at", "tx_index"):
        row.pop(k, None)
    return kind, row


def _before(r: dict) -> tuple[int, int]:
    vsol, vtok = r["virtual_sol_reserves"], r["virtual_token_reserves"]
    if r["is_buy"]:
        return vsol - r["sol_amount"], vtok + r["token_amount"]
    return vsol + r["sol_amount"], vtok - r["token_amount"]


def chain_order(rows: list[dict]) -> list[dict]:
    """One transaction's events in execution order.

    The dataset doesn't always list a transaction's trades on the same token in the order
    they ran. Each trade's reserves before it must equal the previous trade's reserves
    after it, so follow that chain. Creates stay first; if no single chain fits, the
    listed order is kept (the engine then marks the token tainted, as for a missed trade).
    """
    trades = [r for r in rows if r["_kind"] == "trade"]
    if len(trades) < 2:
        return rows
    by_mint: dict[str, list[dict]] = {}
    for r in trades:
        by_mint.setdefault(r["mint"], []).append(r)
    ordered: dict[str, list[dict]] = {}
    for mint, group in by_mint.items():
        if len(group) < 2:
            ordered[mint] = group
            continue
        after = {(r["virtual_sol_reserves"], r["virtual_token_reserves"]) for r in group}
        starts = [r for r in group if _before(r) not in after]
        chain: list[dict] = []
        if len(starts) == 1:
            left = [r for r in group if r is not starts[0]]
            chain = [starts[0]]
            while left:
                tail = chain[-1]
                state = (tail["virtual_sol_reserves"], tail["virtual_token_reserves"])
                nxt = next((r for r in left if _before(r) == state), None)
                if nxt is None:
                    break
                chain.append(nxt)
                left.remove(nxt)
            if left:
                chain = []
        ordered[mint] = chain or group
    # Keep each token's slots in the transaction; fill them in chain order.
    cursor = {m: iter(rs) for m, rs in ordered.items()}
    return [r if r["_kind"] != "trade" else next(cursor[r["mint"]]) for r in rows]
