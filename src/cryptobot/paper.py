"""Paper trader (M6): strategy variants on the live Pump.fun stream, with simulated fills.

Read-only. Nothing here builds, signs or sends a transaction, and no money moves. The
only secret it could touch is an optional CRYPTOBOT_WS_URL, which is never printed.

    uv run python -m cryptobot.paper live [--minutes N]    # stream, record, paper trade
    uv run python -m cryptobot.paper replay [--events DIR]  # same engine on recorded events
    uv run python -m cryptobot.m6_report                    # report on the latest run

One engine serves both modes. Live mode feeds it from recorder.record and saves the raw
events in the run's events/ folder; replay mode feeds it the same rows from disk in
arrival order. A live session therefore replays to the same decisions.

The fill model is replay.py's, applied to each (variant, token) pair:
- A decision is made on a trade event at its arrival time t and lands at t + delay,
  after every trade that arrived at or before then.
- It fills on the curve those trades left, including our own earlier impact. Later
  recorded trades replay on top of our moved curve by their inputs: buys by the SOL
  that reached the curve, sells by the tokens sold.
- Stops fill at the landing price. Fees are charged on the SOL the curve takes or pays,
  network costs are fixed lamports per transaction, and rent comes back only if the
  token account is closed.
- A position still open at graduation or at the end is valued by selling into the
  final curve.
tests/test_paper.py checks that this engine and replay.replay() give identical trades.

Beyond replay.py:
- Each variant has its own wallet (1 SOL by default); a stake is reserved when the
  order is sent.
- Tokens are tracked only from their create event, so Final Stretch entries wait until
  the run is 40 minutes old: before then the column's market-cap ranking is incomplete.
- V1's X-link and top-5 holder checks are looked up live (holders.py) once a token
  passes every other rule, and entries wait for the answer. The same lookup asks a free
  LLM to rate the token's narrative when a key is set (narrative.py). The answers are
  saved with the run's events, so a replay sees exactly what the live run saw.
- Results carry flags: "gap" when the stream dropped while the order or position was
  open, and "missed_trade" when the token's reserves skipped a trade. Tainted tokens
  get no new entries.
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.costs import Venue
from cryptobot.curve import Curve, to_lamports, to_sol
from cryptobot.features import CopycatWindow, TokenState, is_classic
from cryptobot.holders import LOOKUP_SCHEMA, PUBLIC_RPC, TOP_N, LookupWorker, classify
from cryptobot.narrative import make_judge
from cryptobot.recorder import LIVE, PUBLIC_WS, SCHEMAS, Sink, next_seq, now_ms, record
from cryptobot.replay import Buy, Position, Sell, TradeResult, View
from cryptobot.v1 import (
    FINAL_STRETCH_MAX_AGE_MS,
    HARD_MAX_STAKE,
    Universe,
    Variant,
    check,
    column_window,
    default_variants,
    passes,
)

PAPER = LIVE.parent / "paper"
START_BALANCE = to_lamports(1.0)
FLUSH_EVERY_MS = 60_000
PRUNE_AFTER_MS = 60 * 60 * 1000  # forget tokens this old that hold no position

OUT_SCHEMAS = {
    "decisions": {
        "ts_ms": pl.Int64,
        "variant": pl.Utf8,
        "mint": pl.Utf8,
        "kind": pl.Utf8,
        "reason": pl.Utf8,
        "price": pl.Float64,  # lamports per raw token
        "balance_lamports": pl.Int64,
    },
    "trades": {
        "variant": pl.Utf8,
        "universe": pl.Utf8,
        "mint": pl.Utf8,
        "stake_lamports": pl.Int64,
        "entry_decided_ts_ms": pl.Int64,
        "entry_ts_ms": pl.Int64,
        "exit_decided_ts_ms": pl.Int64,
        "exit_ts_ms": pl.Int64,
        "entry_price": pl.Float64,
        "exit_price": pl.Float64,
        "cost_lamports": pl.Int64,
        "proceeds_lamports": pl.Int64,
        "pnl_lamports": pl.Int64,
        "net_return": pl.Float64,
        "entry_reason": pl.Utf8,
        "exit_reason": pl.Utf8,
        "flags": pl.Utf8,
    },
    "gates": {
        "ts_ms": pl.Int64,
        "mint": pl.Utf8,
        "universe": pl.Utf8,
        "passed": pl.Boolean,
        "checks": pl.Utf8,  # JSON: rule -> true / false / null (not evaluated)
        "features": pl.Utf8,  # JSON: TokenState.snapshot()
    },
}


EVENT_SCHEMAS = {**SCHEMAS, "lookups": LOOKUP_SCHEMA}


def _fee(lamports: int, venue: Venue) -> int:
    return -(-int(lamports * venue.fee_bps_per_side) // 10_000)


@dataclass
class _Pending:
    order: Buy | Sell
    decided_ts_ms: int
    land_ts_ms: int
    reserved: int = 0  # lamports held back from the variant's wallet for a buy


@dataclass
class _Slot:
    """One variant on one token."""

    curve: Curve | None = None  # our own copy once our fills move it; None = the real curve
    peak: float = 0.0  # peak of our copy (set when it diverges)
    position: Position | None = None
    pending: _Pending | None = None
    entries: int = 0
    done: bool = False
    watch: str | None = None  # largest holder at entry, for the holder-sells exit
    flags: set[str] = field(default_factory=set)


class Engine:
    def __init__(
        self,
        variants: list[Variant],
        out: Sink | None = None,
        start_balance: int = START_BALANCE,
        unlimited_balance: bool = False,
    ) -> None:
        self.variants = list(variants)
        self.out = out
        self.unlimited = unlimited_balance
        self.balance = [start_balance] * len(self.variants)
        by_name = {v.universe.name: v.universe for v in self.variants}
        self.universes: list[Universe] = list(by_name.values())
        self.tokens: dict[str, TokenState] = {}
        self.copycats = CopycatWindow()
        self.slots: dict[tuple[int, str], _Slot] = {}
        self.slot_index: dict[str, set[int]] = defaultdict(set)
        # Slots with an order in flight, a position, or their own curve.
        self.active: dict[str, set[int]] = defaultdict(set)
        self.queue: list[tuple[int, int, int, str]] = []
        self._tick = itertools.count()
        self.results: list[tuple[int, str, TradeResult]] = []
        self.gates: dict[tuple[str, str], bool] = {}
        self.columns: dict[str, dict[str, tuple[float, int]]] = {
            u.name: {} for u in self.universes if u.kind == "final_stretch"
        }
        self.stats: Counter[str] = Counter()
        self.first_ts: int | None = None
        self.clock = 0
        self.last_flush: int | None = None
        # Lookups for V1's X-link and holder checks. Live mode sets request_lookup;
        # replay feeds recorded answers through on_lookup.
        self.holder_checks = False
        # Called with (mint, top holders, metadata uri, ts, name, symbol).
        self.request_lookup: Callable[[str, list[str], str, int, str, str], None] | None = None
        self.lookups: dict[str, dict] = {}
        self.requested: set[str] = set()
        # Rewritten on every flush, so a report can read a run that is still going.
        self.summary_path: Path | None = None
        self.summary_extra: dict = {}

    # Events -----------------------------------------------------------------

    def on_rows(self, creates: list[dict], trades: list[dict]) -> None:
        """One transaction's events, as the recorder delivers them live."""
        events = [("create", r) for r in creates] + [("trade", r) for r in trades]
        for kind, row in sorted(events, key=lambda e: e[1]["event_index"]):
            if kind == "create":
                self.on_create(row)
            else:
                self.on_trade(row)

    def on_create(self, row: dict) -> None:
        self._advance(row["received_ms"])
        self.stats["creates"] += 1
        copycat = self.copycats.seen(row.get("name"), row.get("symbol"), row["received_ms"])
        if is_classic(row):
            tok = self.tokens[row["mint"]] = TokenState.from_create(row)
            tok.copycat = copycat
            self.stats["classic_creates"] += 1
            self.stats["copycats"] += copycat

    def on_trade(self, row: dict) -> None:
        ts = row["received_ms"]
        self._advance(ts)
        tok = self.tokens.get(row["mint"])
        if tok is None:
            return
        self.stats["trades_on_tracked_tokens"] += 1
        mint = tok.mint
        was_tainted = tok.tainted
        tok.apply_trade(row)
        if tok.tainted and not was_tainted:
            self.stats[f"tainted_{tok.tainted}"] += 1
            for vi in self.active[mint]:
                self.slots[(vi, mint)].flags.add(tok.tainted)

        for vi in list(self.active[mint]):
            slot = self.slots[(vi, mint)]
            if slot.curve is None or slot.done:
                continue
            if row["is_buy"]:
                slot.curve, _, _ = slot.curve.after_buy(row["sol_amount"])
            else:
                slot.curve, _ = slot.curve.after_sell(row["token_amount"])
            slot.peak = max(slot.peak, slot.curve.price())
            if slot.curve.graduated:
                self._close_out(vi, mint, slot, "graduated")

        if tok.curve.graduated:
            for vi in list(self.active[mint]):
                self._close_out(vi, mint, self.slots[(vi, mint)], "graduated")
            self.stats["graduated"] += 1
            self._drop(mint)
            return

        ok = {u.name: self._gate(u, tok, ts) for u in self.universes}
        for vi, var in enumerate(self.variants):
            slot = self.slots.get((vi, mint))
            if slot is None:
                if not ok[var.universe.name]:
                    continue
                slot = self.slots[(vi, mint)] = _Slot()
                self.slot_index[mint].add(vi)
            if not slot.done:
                self._decide(vi, var, tok, slot, ts, row, ok[var.universe.name])

    def on_gap(self, ts: int) -> None:
        """The stream dropped at `ts`: events after it may be missing."""
        self._advance(ts)
        self.stats["gaps"] += 1
        for tok in self.tokens.values():
            tok.tainted = tok.tainted or "gap"
        for slot in self.slots.values():
            if slot.position is not None or slot.pending is not None:
                slot.flags.add("gap")
        self._decision(
            ts, None, None, "gap", "stream dropped; open tokens get no new entries", None
        )

    def on_lookup(self, row: dict) -> None:
        """A recorded or live answer to a lookup request."""
        self._advance(row["received_ms"])
        self.stats["lookups_done" if row["error"] is None else "lookup_errors"] += 1
        if row["mint"] in self.tokens:
            self.lookups[row["mint"]] = row

    def finish(self) -> None:
        """End of data: land orders in flight, then value open positions."""
        for (vi, mint), slot in list(self.slots.items()):
            if slot.done:
                continue
            tok = self.tokens[mint]
            if slot.pending is not None and not self._curve(slot, tok).graduated:
                self._land(vi, mint, slot)
            if not slot.done:
                flag = "graduated" if self._curve(slot, tok).graduated else "open_at_end"
                self._close_out(vi, mint, slot, flag)
        self.flush()

    # Decisions and fills ----------------------------------------------------

    @staticmethod
    def _curve(slot: _Slot, tok: TokenState) -> Curve:
        return slot.curve if slot.curve is not None else tok.curve

    def _gate(self, u: Universe, tok: TokenState, ts: int) -> bool:
        if u.kind == "all":
            return True
        checks = check(u, tok, ts)
        if u.kind == "final_stretch":
            members = self.columns[u.name]
            if passes(checks):
                members[tok.mint] = (tok.mcap_multiple, tok.created_ms)
            else:
                members.pop(tok.mint, None)
            # The column holds tokens up to 40 minutes old, and we only know tokens created
            # since the run began, so the ranking is incomplete for the first 40 minutes.
            assert self.first_ts is not None
            checks["column_complete"] = ts - self.first_ts >= FINAL_STRETCH_MAX_AGE_MS
            checks["column_bottom_four_not_top_two"] = tok.mint in column_window(members, ts)
        if u.kind != "all" and self.holder_checks:
            row = self.lookups.get(tok.mint)
            if row is None:
                if passes(checks) and tok.mint not in self.requested:
                    self.requested.add(tok.mint)
                    self.stats["lookups_requested"] += 1
                    if self.request_lookup is not None:
                        self.request_lookup(
                            tok.mint, tok.top_holders(TOP_N), tok.uri, ts, tok.name, tok.symbol
                        )
                checks["lookup_done"] = False
            elif row["error"] is None:
                checks.update(classify(json.loads(row["result"]), row["received_ms"]))
        ok = passes(checks)
        key = (tok.mint, u.name)
        if self.gates.get(key) != ok:
            self.gates[key] = ok
            self._emit(
                "gates",
                {
                    "ts_ms": ts,
                    "mint": tok.mint,
                    "universe": u.name,
                    "passed": ok,
                    "checks": json.dumps(checks),
                    "features": json.dumps(tok.snapshot(ts)),
                },
            )
        return ok

    def _decide(
        self, vi: int, var: Variant, tok: TokenState, slot: _Slot, ts: int, row: dict, gate_ok: bool
    ) -> None:
        if slot.pending is not None:
            return
        curve = self._curve(slot, tok)
        peak = slot.peak if slot.curve is not None else tok.peak_price
        view = View(ts, tok.created_ms, curve, peak, slot.position, False)
        if slot.position is None:
            if not gate_ok or slot.entries >= var.fill.max_trades_per_token:
                return
            action = var.strategy.decide(view)
            if not isinstance(action, Buy):
                return
            if action.stake_lamports > HARD_MAX_STAKE:
                raise ValueError(f"{var.name}: stake above the 1.5 SOL hard cap")
            need = action.stake_lamports + var.fill.network_lamports_per_tx + var.fill.rent_lamports
            if not self.unlimited:
                if self.balance[vi] < need:
                    self._decision(ts, vi, tok.mint, "skip_balance", action.reason, curve.price())
                    return
                self.balance[vi] -= need
            reserved = 0 if self.unlimited else need
            self._schedule(vi, tok.mint, slot, action, ts, var.fill.delay_ms, reserved)
            self._decision(ts, vi, tok.mint, "signal_buy", action.reason, curve.price())
            return
        action = var.strategy.decide(view)
        if action is None and var.holder_sell and not row["is_buy"] and row["user"] == slot.watch:
            action = Sell("largest holder sold")
        if isinstance(action, Sell):
            self._schedule(vi, tok.mint, slot, action, ts, var.fill.delay_ms)
            self._decision(ts, vi, tok.mint, "signal_sell", action.reason, curve.price())

    def _schedule(
        self,
        vi: int,
        mint: str,
        slot: _Slot,
        order: Buy | Sell,
        ts: int,
        delay: int,
        reserved: int = 0,
    ) -> None:
        slot.pending = _Pending(order, ts, ts + delay, reserved)
        heapq.heappush(self.queue, (ts + delay, next(self._tick), vi, mint))
        self.active[mint].add(vi)

    def _advance(self, ts: int) -> None:
        """Land every order whose landing time is before `ts`, then maybe flush."""
        if self.first_ts is None:
            self.first_ts = ts
        while self.queue and self.queue[0][0] < ts:
            land, _, vi, mint = heapq.heappop(self.queue)
            slot = self.slots.get((vi, mint))
            if slot and slot.pending and slot.pending.land_ts_ms == land and not slot.done:
                self._land(vi, mint, slot)
        self.clock = max(self.clock, ts)
        if self.out is not None:
            if self.last_flush is None:
                self.last_flush = ts
            elif ts - self.last_flush >= FLUSH_EVERY_MS:
                self.flush()
                self.last_flush = ts

    def _land(self, vi: int, mint: str, slot: _Slot) -> None:
        p, slot.pending = slot.pending, None
        assert p is not None
        var, tok = self.variants[vi], self.tokens[mint]
        if isinstance(p.order, Buy) and slot.position is None:
            self._fill_buy(vi, var, tok, slot, p)
        elif isinstance(p.order, Sell) and slot.position is not None:
            self._sell(vi, var, tok, slot, p, ())
        elif p.reserved:
            self.balance[vi] += p.reserved
        if slot.curve is not None and slot.curve.graduated:
            self._close_out(vi, mint, slot, "graduated")

    def _fill_buy(self, vi: int, var: Variant, tok: TokenState, slot: _Slot, p: _Pending) -> None:
        cfg = var.fill
        order = p.order
        assert isinstance(order, Buy)
        to_curve = int(order.stake_lamports / (1 + cfg.venue.fee_rate))
        curve, tokens, spent = self._curve(slot, tok).after_buy(to_curve)
        if tokens == 0:
            self.balance[vi] += p.reserved
            self._decision(p.land_ts_ms, vi, tok.mint, "no_fill", order.reason, None)
            return
        cost = spent + _fee(spent, cfg.venue) + cfg.network_lamports_per_tx + cfg.rent_lamports
        if slot.curve is None:
            slot.peak = tok.peak_price
        slot.curve = curve
        slot.position = Position(
            p.land_ts_ms, tokens, cost, spent / tokens, p.decided_ts_ms, order.reason
        )
        slot.entries += 1
        if not self.unlimited:
            self.balance[vi] += p.reserved - cost
        slot.watch = tok.largest_holder()[0]
        self._decision(p.land_ts_ms, vi, tok.mint, "fill_buy", order.reason, spent / tokens)

    def _sell(
        self,
        vi: int,
        var: Variant,
        tok: TokenState,
        slot: _Slot,
        p: _Pending,
        flags: tuple[str, ...],
    ) -> None:
        cfg = var.fill
        pos = slot.position
        assert pos is not None and slot.curve is not None
        slot.curve, gross = slot.curve.after_sell(pos.tokens)
        proceeds = gross - _fee(gross, cfg.venue) - cfg.network_lamports_per_tx
        if cfg.rent_refunded:
            proceeds += cfg.rent_lamports
        reason = p.order.reason if isinstance(p.order, Sell) else ""
        result = TradeResult(
            entry_decided_ts_ms=pos.decided_ts_ms,
            entry_ts_ms=pos.entry_ts_ms,
            exit_decided_ts_ms=p.decided_ts_ms,
            exit_ts_ms=p.land_ts_ms,
            entry_price=pos.entry_price,
            exit_price=gross / pos.tokens,
            cost_lamports=pos.cost_lamports,
            proceeds_lamports=proceeds,
            entry_reason=pos.reason,
            exit_reason=reason,
            flags=flags + tuple(sorted(slot.flags - set(flags))),
        )
        self.results.append((vi, tok.mint, result))
        if not self.unlimited:
            self.balance[vi] += proceeds
        slot.position = None
        slot.flags.clear()
        self._decision(p.land_ts_ms, vi, tok.mint, "fill_sell", reason, result.exit_price)
        self._emit(
            "trades",
            {
                "variant": var.name,
                "universe": var.universe.name,
                "mint": tok.mint,
                "stake_lamports": getattr(var.strategy, "stake_lamports", None),
                "entry_decided_ts_ms": result.entry_decided_ts_ms,
                "entry_ts_ms": result.entry_ts_ms,
                "exit_decided_ts_ms": result.exit_decided_ts_ms,
                "exit_ts_ms": result.exit_ts_ms,
                "entry_price": result.entry_price,
                "exit_price": result.exit_price,
                "cost_lamports": result.cost_lamports,
                "proceeds_lamports": result.proceeds_lamports,
                "pnl_lamports": result.pnl_lamports,
                "net_return": result.net_return,
                "entry_reason": result.entry_reason,
                "exit_reason": result.exit_reason,
                "flags": ",".join(result.flags),
            },
        )
        if slot.entries >= cfg.max_trades_per_token:
            slot.done = True
            self.active[tok.mint].discard(vi)

    def _close_out(self, vi: int, mint: str, slot: _Slot, flag: str) -> None:
        """Stop trading this token: drop any order, value any position at the last curve."""
        if slot.done:
            return
        if slot.pending is not None:
            self.balance[vi] += slot.pending.reserved
            slot.pending = None
        if slot.position is not None:
            tok = self.tokens[mint]
            p = _Pending(Sell(flag), tok.last_ts_ms, tok.last_ts_ms)
            self._sell(vi, self.variants[vi], tok, slot, p, (flag,))
        slot.done = True
        self.active[mint].discard(vi)

    # Output -----------------------------------------------------------------

    def _emit(self, table: str, row: dict) -> None:
        if self.out is not None:
            self.out.add(table, [row])

    def _decision(
        self,
        ts: int,
        vi: int | None,
        mint: str | None,
        kind: str,
        reason: str,
        price: float | None,
    ) -> None:
        self._emit(
            "decisions",
            {
                "ts_ms": ts,
                "variant": None if vi is None else self.variants[vi].name,
                "mint": mint,
                "kind": kind,
                "reason": reason,
                "price": price,
                "balance_lamports": None if vi is None else self.balance[vi],
            },
        )

    def _drop(self, mint: str) -> None:
        self.tokens.pop(mint, None)
        for vi in self.slot_index.pop(mint, set()):
            self.slots.pop((vi, mint), None)
        self.active.pop(mint, None)
        self.lookups.pop(mint, None)
        self.requested.discard(mint)
        for u in self.universes:
            self.gates.pop((mint, u.name), None)
        for members in self.columns.values():
            members.pop(mint, None)

    def flush(self) -> None:
        stale = [
            m
            for m, t in self.tokens.items()
            if self.clock - t.created_ms > PRUNE_AFTER_MS and not self.active.get(m)
        ]
        for mint in stale:
            self._drop(mint)
        if self.out is not None:
            self.out.flush()
        self.write_summary()

    def write_summary(self) -> None:
        if self.summary_path is None:
            return
        tmp = self.summary_path.with_name(self.summary_path.name + ".tmp")
        tmp.write_text(json.dumps({**self.summary_extra, **self.summary()}, indent=1))
        os.replace(tmp, self.summary_path)

    def summary(self) -> dict:
        return {
            "first_event_ms": self.first_ts,
            "last_event_ms": self.clock,
            "stats": dict(self.stats),
            "variants": len(self.variants),
            "closed_trades": len(self.results),
            "final_balance_lamports": {
                v.name: self.balance[i] for i, v in enumerate(self.variants)
            },
        }


# Sources ----------------------------------------------------------------------


def read_table(root: Path, table: str) -> pl.DataFrame | None:
    """All Parquet files of one table under a recorder or paper folder, or None."""
    files = sorted((root / table).glob("*/*.parquet"))
    if not files:
        return None
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def recorded_events(root: Path = LIVE) -> Iterator[tuple[str, dict | int]]:
    """Create, trade and gap events from recorder files, in the order they arrived.

    A gap is every connection end except the last: the stream was down until the next
    connection began.
    """
    frames = []
    for kind, table in (("create", "create"), ("trade", "trade"), ("lookup", "lookups")):
        df = read_table(root, table)
        if df is not None:
            frames.append(df.with_columns(_kind=pl.lit(kind)))
    if not frames:
        return
    events = pl.concat(frames, how="diagonal_relaxed")
    if "seq" not in events.columns:
        events = events.with_columns(seq=pl.lit(None, pl.UInt64))
    events = events.sort(
        ["received_ms", "seq", "event_index"], nulls_last=True, maintain_order=True
    )
    sessions = read_table(root, "sessions")
    gaps = [] if sessions is None else sessions.sort("connected_ms")["disconnected_ms"].to_list()
    gaps = gaps[:-1]
    g = 0
    for row in events.iter_rows(named=True):
        while g < len(gaps) and gaps[g] < row["received_ms"]:
            yield "gap", gaps[g]
            g += 1
        kind = row.pop("_kind")
        yield kind, row


def run_replay(engine: Engine, root: Path = LIVE) -> None:
    # Lookup checks apply only if the recording has the answers.
    engine.holder_checks = read_table(root, "lookups") is not None
    for kind, item in recorded_events(root):
        if kind == "gap":
            assert isinstance(item, int)
            engine.on_gap(item)
        elif kind == "lookup":
            assert isinstance(item, dict)
            engine.on_lookup(item)
        elif kind == "create":
            assert isinstance(item, dict)
            engine.on_create(item)
        else:
            assert isinstance(item, dict)
            engine.on_trade(item)
    engine.finish()


def run_live(engine: Engine, minutes: float | None, events_dir: Path) -> None:
    url = os.environ.get("CRYPTOBOT_WS_URL") or PUBLIC_WS
    source = "CRYPTOBOT_WS_URL" if url != PUBLIC_WS else PUBLIC_WS
    print("paper trading live from", source, flush=True)
    rpc_url = os.environ.get("CRYPTOBOT_RPC_URL") or PUBLIC_RPC
    sink = Sink(events_dir, EVENT_SCHEMAS)
    judge = make_judge()
    print(
        "narrative judge:",
        ", ".join(judge.names) if judge else "none (no GROQ_API_KEY or GEMINI_API_KEY)",
        flush=True,
    )
    worker = LookupWorker(
        engine.on_lookup, lambda row: sink.add("lookups", [row]), next_seq, now_ms, rpc_url, judge
    )
    engine.holder_checks = True
    engine.request_lookup = worker.request

    async def stream() -> None:
        task = asyncio.create_task(worker.run())
        try:
            await record(minutes, url, sink, engine.on_rows, engine.on_gap)
        finally:
            task.cancel()

    try:
        asyncio.run(stream())
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        engine.finish()
        sink.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    live = modes.add_parser("live", help="stream, record and paper trade")
    live.add_argument("--minutes", type=float, help="stop after this long (default: until Ctrl-C)")
    rep = modes.add_parser("replay", help="paper trade recorded events")
    rep.add_argument(
        "--events", type=Path, default=LIVE, help="recorder folder (default data/live)"
    )
    for p in (live, rep):
        p.add_argument(
            "--max-stake-sol", type=float, default=1.0, help="per-trade cap, at most 1.5"
        )
    args = parser.parse_args(argv)

    suffix = "" if args.mode == "live" else "-replay"
    run_dir = PAPER / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + suffix)
    variants = default_variants(to_lamports(args.max_stake_sol))
    run_dir.mkdir(parents=True)
    (run_dir / "variants.json").write_text(json.dumps([v.describe() for v in variants], indent=1))
    engine = Engine(variants, Sink(run_dir, OUT_SCHEMAS))
    source = run_dir / "events" if args.mode == "live" else args.events
    engine.summary_path = run_dir / "summary.json"
    engine.summary_extra = {"mode": args.mode, "source": str(source), "running": True}
    if args.mode == "live":
        run_live(engine, args.minutes, source)
    else:
        run_replay(engine, source)
    engine.summary_extra["running"] = False
    engine.write_summary()
    pnl = sum(r.pnl_lamports for _, _, r in engine.results)
    print(f"{run_dir}: {len(engine.results)} closed paper trades, {to_sol(pnl):+.4f} SOL in total")


if __name__ == "__main__":
    main()
