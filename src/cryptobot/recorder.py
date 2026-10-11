"""Record live Pump.fun create and trade events to Parquet (M5).

Run with `uv run python -m cryptobot.recorder [--minutes N]`; Ctrl-C stops it cleanly.
Read-only: it subscribes to the program's logs and never signs or sends anything.

Files land in data/live/<table>/<UTC date>/<HHMMSS>.parquet for the tables `create`,
`trade` and `sessions`. A `sessions` row is written for every connection, with its
start, end and counts of transactions, failed transactions and truncated logs, so
gaps in coverage are in the data, not guessed at later.

The stream comes from the free public RPC unless CRYPTOBOT_WS_URL is set (for a
keyed RPC the owner puts in .env). That URL may hold a key, so it is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.events import CREATE_FIELDS, TRADE_FIELDS, events_from_logs

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
LIVE = Path(__file__).resolve().parents[2] / "data" / "live"
FLUSH_SECONDS = 60

_DTYPES = {
    "pubkey": pl.Utf8,
    "string": pl.Utf8,
    "u64": pl.UInt64,
    "i64": pl.Int64,
    "u8": pl.UInt8,
    "bool": pl.Boolean,
}
_META = {
    "seq": pl.UInt64,
    "signature": pl.Utf8,
    "slot": pl.UInt64,
    "received_ms": pl.Int64,
    "event_index": pl.UInt16,
}

SCHEMAS = {
    "create": {**_META, **{n: _DTYPES[k] for n, k in CREATE_FIELDS}},
    "trade": {**_META, **{n: _DTYPES[k] for n, k in TRADE_FIELDS}},
    "sessions": {
        "connected_ms": pl.Int64,
        "disconnected_ms": pl.Int64,
        "txs": pl.UInt64,
        "failed_txs": pl.UInt64,
        "truncated_txs": pl.UInt64,
        "end_reason": pl.Utf8,
    },
}


# Arrival order of notifications in this process. With `event_index` it orders every
# event exactly as it arrived, which millisecond timestamps alone cannot.
_SEQ = itertools.count()

RowsCallback = Callable[[list[dict], list[dict]], None]
GapCallback = Callable[[int], None]


class ConsumerError(RuntimeError):
    """A callback failed. Stop recording instead of treating it as a dropped connection."""


def next_seq() -> int:
    """The next arrival number, shared by every event this process records."""
    return next(_SEQ)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def rows_from_notification(
    msg: dict, received_ms: int, seq: int = 0
) -> tuple[list[dict], list[dict], bool]:
    """Split one logsNotification into create rows, trade rows and a truncated-logs flag.

    Failed transactions are skipped: their events never took effect.
    """
    result = msg["params"]["result"]
    value = result["value"]
    if value.get("err") is not None:
        return [], [], False
    logs = value.get("logs") or []
    meta = {"seq": seq, "signature": value["signature"], "slot": result["context"]["slot"]}
    creates, trades = [], []
    for i, (kind, fields) in enumerate(events_from_logs(logs)):
        row = {**meta, "received_ms": received_ms, "event_index": i, **fields}
        (creates if kind == "create" else trades).append(row)
    truncated = any("Log truncated" in line for line in logs)
    return creates, trades, truncated


class Sink:
    """Buffers rows per table and writes them as Parquet files."""

    def __init__(self, root: Path = LIVE, schemas: dict[str, dict] = SCHEMAS) -> None:
        self.root = root
        self.schemas = schemas
        self.rows: dict[str, list[dict]] = {t: [] for t in schemas}

    def add(self, table: str, rows: list[dict]) -> None:
        self.rows[table].extend(rows)

    def flush(self) -> list[Path]:
        written = []
        stamp = datetime.now(UTC)
        for table, rows in self.rows.items():
            if not rows:
                continue
            path = self.root / table / f"{stamp:%Y-%m-%d}" / f"{stamp:%H%M%S%f}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a reader never sees a half-written file.
            tmp = path.with_name(path.name + ".tmp")
            pl.DataFrame(rows, schema=self.schemas[table], strict=False).write_parquet(tmp)
            os.replace(tmp, path)
            written.append(path)
            self.rows[table] = []
        return written


async def _session(
    url: str, sink: Sink, deadline: float, stats: dict, on_rows: RowsCallback | None
) -> None:
    import websockets

    async with websockets.connect(url, max_size=2**24, ping_interval=20) as ws:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}],
        }
        await ws.send(json.dumps(request))
        reply = json.loads(await ws.recv())
        if "error" in reply:
            raise RuntimeError(f"logsSubscribe refused: {reply['error']}")
        last_flush = time.monotonic()
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if msg.get("method") != "logsNotification":
                continue
            creates, trades, truncated = rows_from_notification(msg, now_ms(), next(_SEQ))
            sink.add("create", creates)
            sink.add("trade", trades)
            if on_rows is not None:
                try:
                    on_rows(creates, trades)
                except Exception as exc:
                    raise ConsumerError("on_rows failed") from exc
            stats["txs"] += 1
            stats["failed_txs"] += msg["params"]["result"]["value"].get("err") is not None
            stats["truncated_txs"] += truncated
            if time.monotonic() - last_flush >= FLUSH_SECONDS:
                sink.flush()
                last_flush = time.monotonic()


async def record(
    minutes: float | None,
    url: str,
    sink: Sink,
    on_rows: RowsCallback | None = None,
    on_gap: GapCallback | None = None,
) -> None:
    """Stream until the deadline, reconnecting on drops.

    `on_rows` gets each transaction's create and trade rows as they arrive. `on_gap`
    gets the time a connection dropped, when the next connection starts, so a consumer
    knows events may be missing (the same gaps paper.recorded_events finds on disk).
    """
    deadline = time.monotonic() + minutes * 60 if minutes else float("inf")
    backoff = 1
    gap: int | None = None  # end of the last dropped connection, reported on reconnect
    while time.monotonic() < deadline:
        if gap is not None and on_gap is not None:
            on_gap(gap)
        gap = None
        stats = {"connected_ms": now_ms(), "txs": 0, "failed_txs": 0, "truncated_txs": 0}
        reason = "deadline"
        try:
            await _session(url, sink, deadline, stats, on_rows)
        except asyncio.CancelledError:
            reason = "stopped"
            raise
        except ConsumerError:
            reason = "consumer_error"
            raise
        except Exception as exc:  # any drop means reconnect; the reason is logged
            reason = type(exc).__name__
        finally:
            ended = now_ms()
            sink.add("sessions", [{**stats, "disconnected_ms": ended, "end_reason": reason}])
            sink.flush()
            print(f"session ended ({reason}): {stats['txs']:,} txs", flush=True)
        if reason == "deadline":
            break
        gap = ended
        backoff = 1 if stats["txs"] else min(backoff * 2, 60)
        await asyncio.sleep(backoff)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes", type=float, help="stop after this long (default: until Ctrl-C)"
    )
    args = parser.parse_args()
    url = os.environ.get("CRYPTOBOT_WS_URL") or PUBLIC_WS
    print("recording from", "CRYPTOBOT_WS_URL" if url != PUBLIC_WS else PUBLIC_WS, flush=True)
    try:
        asyncio.run(record(args.minutes, url, Sink()))
    except KeyboardInterrupt:
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
