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
- The conviction universe scores each token 0-100 (conviction.py) from its state and its
  lookup, and its variants take their entry drop from the score's tier at decision time.
  Scores use only events that arrived before the decision, so replays match live runs.
- summary.json marks open positions to market: what selling them into the current curve
  would bring after fees, without touching the engine's state.
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.conviction import (
    OFFLINE,
    Conviction,
    blocks,
    control_pick,
    conviction,
    elite_gate,
    label_entry,
    narrative_gate,
)
from cryptobot.costs import Venue
from cryptobot.creators import CREATORS_PATH, RECENT_LAUNCH_MS
from cryptobot.creators import load as load_creators
from cryptobot.curve import Curve, to_lamports, to_sol
from cryptobot.features import CopycatWindow, TokenState, is_classic
from cryptobot.holders import LOOKUP_SCHEMA, PUBLIC_RPC, TOP_N, LookupWorker, classify
from cryptobot.narrative import make_judge
from cryptobot.recorder import LIVE, PUBLIC_WS, SCHEMAS, Sink, next_seq, now_ms, record
from cryptobot.replay import TICK_MS, Buy, FillConfig, Position, Sell, TradeResult, View
from cryptobot.social_state import NOW_PATH as SOCIAL_NOW
from cryptobot.social_state import signals as social_signals
from cryptobot.tracked import TRACKED_PATH, load_list, load_top
from cryptobot.v1 import (
    FINAL_STRETCH_MAX_AGE_MS,
    HARD_MAX_STAKE,
    LEADER_TOP,
    Universe,
    Variant,
    check,
    column_window,
    default_variants,
    passes,
)
from cryptobot.watch import WATCH_PATH as SOCIAL_WATCH

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


# Each tracked-wallet list the live run loaded, so a replay uses the same lists.
TRACKED_SCHEMA = {
    "seq": pl.UInt64,
    "received_ms": pl.Int64,
    "event_index": pl.UInt16,
    "list_mtime_ms": pl.Int64,
    "wallets": pl.List(pl.Utf8),
    "top": pl.List(pl.Utf8),  # the most profitable, in order (late_p90_leader); may be null
}
# Each creator-record file the live run loaded (creators.py), as parallel lists.
CREATORS_SCHEMA = {
    "seq": pl.UInt64,
    "received_ms": pl.Int64,
    "event_index": pl.UInt16,
    "list_mtime_ms": pl.Int64,
    "creators": pl.List(pl.Utf8),
    "launches": pl.List(pl.Int64),
    "graduated": pl.List(pl.Int64),
    "last_launch_ms": pl.List(pl.Int64),
}
SOCIAL_SCHEMA = {
    "seq": pl.UInt64,
    "received_ms": pl.Int64,
    "event_index": pl.UInt16,
    "list_mtime_ms": pl.Int64,
    "state": pl.Utf8,  # social.py's current state, as JSON
}
EVENT_SCHEMAS = {
    **SCHEMAS,
    "lookups": LOOKUP_SCHEMA,
    "tracked": TRACKED_SCHEMA,
    "creators": CREATORS_SCHEMA,
    "social": SOCIAL_SCHEMA,
}
SOCIAL_POLL_MS = 300_000  # the collector rewrites its file every 2 minutes
WATCH_AHEAD = 0.10  # late-curve coins join the watchlist 10 points before the cut-off
TRACKED_POLL_MS = 60_000


def _fee(lamports: int, venue: Venue) -> int:
    return -(-int(lamports * venue.fee_bps_per_side) // 10_000)


def _proceeds(gross: int, cfg: FillConfig) -> int:
    """What a sell paying `gross` lamports out of the curve leaves us, after costs."""
    out = gross - _fee(gross, cfg.venue) - cfg.network_lamports_per_tx
    return out + cfg.rent_lamports if cfg.rent_refunded else out


def _lookup_features(result: dict | None) -> dict:
    """The lookup's social fields for the gates log; creator-written text is truncated."""
    if result is None:
        return {}
    narrative = result.get("narrative")
    return {
        "x_link": result.get("x_link"),
        "x_kind": result.get("x_kind"),
        "narrative": None
        if narrative is None
        else {k: narrative.get(k) for k in ("score", "theme", "red_flags")},
        "narrative_error": result.get("narrative_error"),
        "has_telegram": bool(result.get("telegram")),
        "has_website": bool(result.get("website")),
    }


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
    high: float | None = None  # best price since our position filled


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
        self.offline = False  # backtest on data without lookups: conviction.OFFLINE
        self.flush_every_ms = FLUSH_EVERY_MS  # event time between writes to disk
        self.balance = [start_balance] * len(self.variants)
        by_name = {v.universe.name: v.universe for v in self.variants}
        self.universes: list[Universe] = list(by_name.values())
        self.tokens: dict[str, TokenState] = {}
        self.copycats = CopycatWindow()
        self.slots: dict[tuple[int, str], _Slot] = {}
        self.slot_index: dict[str, set[int]] = defaultdict(set)
        # Slots with an order in flight, a position, or their own curve.
        self.active: dict[str, set[int]] = defaultdict(set)
        # (time, kind, order, variant, mint): kind 0 = clock tick of a token's open
        # positions, 1 = an order landing. Ticks go first at equal times, as in replay.py.
        self.queue: list[tuple[int, int, int, int, str]] = []
        self.next_tick: dict[str, int] = {}
        self._tick = itertools.count()
        self.results: list[tuple[int, str, TradeResult]] = []
        self.keep_results = True  # False for long backtests: trades are on disk anyway
        self.closed_trades = 0
        self.closed_pnl_lamports = 0
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
        self.results_by_mint: dict[str, dict] = {}  # parsed result of each good lookup
        self.convictions: dict[str, Conviction | None] = {}
        # Other conviction-kind universes (narrative-gated, control, labels): name -> mint ->
        self.other_convictions: dict[str, dict[str, Conviction | None]] = {}
        self.requested: set[str] = set()
        # Tracked-wallet list (tracked.py); None until one is loaded.
        self.tracked: frozenset[str] | None = None
        # The most profitable tracked wallets, for the late-curve leader rule.
        self.leaders: frozenset[str] | None = None
        # Late-curve universes: tokens whose curve already crossed the universe's cut-off.
        self.late_crossed: set[tuple[str, str]] = set()
        # Social and attention signals (social.py); None until a state file is loaded.
        self.social: dict | None = None
        # Live runs append late-curve and elite candidates here for social.py's closer
        # (and, for X, paid) look; None in replays and backtests.
        self.watch_path: Path | None = None
        self.watched: set[str] = set()
        # Creator records (creators.py): creator -> (launches, graduated, last launch ms).
        self.creators: dict[str, tuple[int, int, int]] | None = None
        self.creator_launches: dict[str, int] = {}  # creator -> newest launch seen live
        self.creator_pruned_ms = 0
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
            self._stamp_creator(tok)
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
            if slot.high is not None:
                slot.high = max(slot.high, slot.curve.price())
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
        self._schedule_tick(mint, ts)

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

    def on_creators(self, row: dict) -> None:
        """A creator-record file took effect (loaded live, or replayed)."""
        self._advance(row["received_ms"])
        self.creators = {
            c: (n, g, t)
            for c, n, g, t in zip(
                row["creators"],
                row["launches"],
                row["graduated"],
                row["last_launch_ms"],
                strict=True,
            )
        }
        self.stats["creator_files"] += 1

    def _stamp_creator(self, tok: TokenState) -> None:
        """Give a new token its creator's record, from the file and launches seen live."""
        rec = self.creators.get(tok.creator) if self.creators is not None else None
        seen = self.creator_launches.get(tok.creator)
        earlier = [t for t in (seen, rec[2] if rec else None) if t is not None]
        earlier = [t for t in earlier if t < tok.created_ms]
        recent = any(tok.created_ms - t <= RECENT_LAUNCH_MS for t in earlier)
        if self.creators is not None:
            tok.creator_graduates = rec[1] if rec else 0
            tok.creator_recent_launch = recent
        elif recent:
            tok.creator_recent_launch = True
        self.creator_launches[tok.creator] = tok.created_ms
        if tok.created_ms - self.creator_pruned_ms >= 3_600_000:  # hourly: forget old ones
            cut = tok.created_ms - RECENT_LAUNCH_MS
            self.creator_launches = {c: t for c, t in self.creator_launches.items() if t >= cut}
            self.creator_pruned_ms = tok.created_ms

    def on_tracked(self, row: dict) -> None:
        """A tracked-wallet list took effect (loaded live, or replayed from the recording)."""
        self._advance(row["received_ms"])
        self.tracked = frozenset(row["wallets"])
        top = row.get("top")
        self.leaders = frozenset(top) if top else None
        self.stats["tracked_lists"] += 1

    def _watch(self, tok: TokenState, ts: int, universe: str) -> None:
        """Put a candidate on the watchlist once (social.Watchlist reads it)."""
        if tok.mint in self.watched or self.watch_path is None:
            return
        self.watched.add(tok.mint)
        line = {"mint": tok.mint, "name": tok.name, "symbol": tok.symbol, "ts_ms": ts}
        line["uri"] = tok.uri  # metadata link, for the coin's own Telegram (tgweb.py)
        line["universe"] = universe
        with self.watch_path.open("a") as f:
            f.write(json.dumps(line) + "\n")

    def on_social(self, row: dict) -> None:
        """A social-signal state took effect (loaded live, or replayed)."""
        self._advance(row["received_ms"])
        self.social = json.loads(row["state"])
        self.stats["social_states"] += 1

    def on_lookup(self, row: dict) -> None:
        """A recorded or live answer to a lookup request."""
        self._advance(row["received_ms"])
        result = json.loads(row["result"]) if row["result"] is not None else None
        if result is not None and result.get("narrative_update"):
            # The narrative judge's later verdict (holders.LookupWorker): add it to the
            # token's lookup result; the lookup's own time and checks stay as they were.
            self.stats["narratives_done"] += 1
            known = self.results_by_mint.get(row["mint"])
            if known is not None:
                known.update({k: v for k, v in result.items() if k != "narrative_update"})
                known["narrative_done"] = True
            return
        self.stats["lookups_done" if row["error"] is None else "lookup_errors"] += 1
        if row["mint"] in self.tokens:
            self.lookups[row["mint"]] = row
            if row["error"] is None and result is not None:
                self.results_by_mint[row["mint"]] = result

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
        checks = check(u, tok, ts, self.tracked)
        if u.kind == "late":
            # Decide only on the trade that first takes the curve past the cut-off (screen.py's
            # `late` entries); a token that crossed before stays out for good.
            key = (u.name, tok.mint)
            crossed = tok.curve.progress >= u.cutoff
            checks["first_cross_now"] = crossed and key not in self.late_crossed
            if self.watch_path is not None and tok.curve.progress >= u.cutoff - WATCH_AHEAD:
                self._watch(tok, ts, u.name)  # early, so social signals arrive before entry
            if crossed:
                self.late_crossed.add(key)
            if u.leader_top:
                held = self.leaders is not None and any(
                    w in self.leaders for w in tok.balances
                )  # every wallet that bought the token, still holding or not
                checks["top_leader_bought"] = held if self.leaders is not None else False
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
        if u.kind not in ("all", "late") and self.holder_checks:
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
            elif u.kind != "conviction" and tok.mint in self.results_by_mint:
                checks.update(classify(self.results_by_mint[tok.mint], row["received_ms"]))
            elif u.kind != "conviction":
                # The lookup failed or was refused ("busy"): V1's holder and X checks
                # can't be done, and a rule that can't be checked doesn't pass.
                checks["lookup_ok"] = False
        result = self.results_by_mint.get(tok.mint)
        row = self.lookups.get(tok.mint)
        lookup_ms = None if row is None or result is None else row["received_ms"]
        if u.kind == "conviction":
            if self.offline and result is None:
                result, lookup_ms = OFFLINE, ts  # backtest: no lookups (conviction.OFFLINE)
            conv = conviction(tok, result, lookup_ms, ts, measure_only=u.labels)
            if conv is not None and u.narrative_gate:
                assert result is not None
                conv = narrative_gate(conv, result)
            elif conv is not None and u.control:
                conv = control_pick(conv, tok.mint)
            elif conv is not None and u.labels:
                conv = label_entry(conv)
            elif conv is not None and u.elite:
                assert result is not None
                conv = elite_gate(conv, tok, result, u.thesis)
            if u.name == "conviction":
                self.convictions[tok.mint] = conv
            else:
                self.other_convictions.setdefault(u.name, {})[tok.mint] = conv
            checks["conviction_tradeable"] = conv is not None and conv.drop is not None
        ok = passes(checks)
        key = (tok.mint, u.name)
        if ok and self.watch_path is not None and (u.kind == "late" or u.elite):
            self._watch(tok, ts, u.name)
        if self.gates.get(key) != ok:
            self.gates[key] = ok
            features = tok.snapshot(ts) | _lookup_features(result)
            if result is not None and not result.get("offline") and lookup_ms is not None:
                features["lookup_checks"] = classify(result, lookup_ms)
            if self.tracked is not None:
                features["tracked_holders"] = tok.tracked_holders(self.tracked)
            # Logged only, not traded on (CLAUDE.md "Social data"); social.report measures it.
            features["social"] = social_signals(
                self.social, tok.mint, tok.name, tok.symbol, now_ms=ts
            )
            if u.kind == "conviction":
                features["conviction"] = (
                    conv.describe() if conv else {"blocks": blocks(tok, result, lookup_ms, ts)}
                )
            self._emit(
                "gates",
                {
                    "ts_ms": ts,
                    "mint": tok.mint,
                    "universe": u.name,
                    "passed": ok,
                    "checks": json.dumps(checks),
                    "features": json.dumps(features),
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
        view = View(
            ts, tok.created_ms, curve, peak, slot.position, False, high_since_entry=slot.high
        )
        if slot.position is None:
            if not gate_ok or slot.entries >= var.fill.max_trades_per_token:
                return
            strategy = var.strategy
            name = var.universe.name
            store = (
                self.convictions if name == "conviction" else self.other_convictions.get(name, {})
            )
            conv = store.get(tok.mint) if var.conviction else None
            if var.conviction:
                if conv is None or conv.drop is None:
                    return
                strategy = replace(strategy, drop=conv.drop)  # a frozen DipBuyer
            action = strategy.decide(view)
            if not isinstance(action, Buy):
                return
            if conv is not None:
                action = Buy(action.stake_lamports, f"conviction {conv.score} {conv.tier}")
            if action.stake_lamports > HARD_MAX_STAKE:
                raise ValueError(f"{var.name}: stake above the 1.5 SOL hard cap")
            need = action.stake_lamports + var.fill.network_lamports_per_tx + var.fill.rent_lamports
            unlimited = self.unlimited or var.shadow
            if not unlimited:
                if self.balance[vi] < need:
                    self._decision(ts, vi, tok.mint, "skip_balance", action.reason, curve.price())
                    return
                self.balance[vi] -= need
            reserved = 0 if unlimited else need
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
        heapq.heappush(self.queue, (ts + delay, 1, next(self._tick), vi, mint))
        self.active[mint].add(vi)

    def _advance(self, ts: int) -> None:
        """Land every order whose landing time is before `ts`, then maybe flush."""
        if self.first_ts is None:
            self.first_ts = ts
        while self.queue and self.queue[0][0] < ts:
            land, kind, _, vi, mint = heapq.heappop(self.queue)
            if kind == 0:
                self._tick_positions(mint, land)
                continue
            slot = self.slots.get((vi, mint))
            if slot and slot.pending and slot.pending.land_ts_ms == land and not slot.done:
                self._land(vi, mint, slot)
        self.clock = max(self.clock, ts)
        if self.out is not None:
            if self.last_flush is None:
                self.last_flush = ts
            elif ts - self.last_flush >= self.flush_every_ms:
                self.flush()
                self.last_flush = ts

    def _schedule_tick(self, mint: str, after_ms: int) -> None:
        """Re-check this token's open positions TICK_MS after `after_ms` if any are live."""
        live = any(
            (s := self.slots.get((vi, mint))) is not None
            and not s.done
            and (s.position is not None or s.pending is not None)
            for vi in self.active.get(mint, ())
        )
        if not live:
            self.next_tick.pop(mint, None)
            return
        t = after_ms + TICK_MS
        self.next_tick[mint] = t
        heapq.heappush(self.queue, (t, 0, next(self._tick), -1, mint))

    def _tick_positions(self, mint: str, t: int) -> None:
        """A clock check with no new trade: time-based exits can fire (replay.py ticks)."""
        if self.next_tick.get(mint) != t or mint not in self.tokens:
            return
        tok = self.tokens[mint]
        for vi in sorted(self.active.get(mint, ())):
            slot = self.slots[(vi, mint)]
            if slot.done or slot.position is None or slot.pending is not None:
                continue
            var = self.variants[vi]
            curve = self._curve(slot, tok)
            peak = slot.peak if slot.curve is not None else tok.peak_price
            view = View(
                t, tok.created_ms, curve, peak, slot.position, False, tok.last_ts_ms, slot.high
            )
            action = var.strategy.decide(view)
            if isinstance(action, Sell):
                self._schedule(vi, mint, slot, action, t, var.fill.delay_ms)
                self._decision(t, vi, mint, "signal_sell", action.reason, curve.price())
        self._schedule_tick(mint, t)

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
        slot.high = curve.price()
        if not (self.unlimited or var.shadow):
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
        proceeds = _proceeds(gross, cfg)
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
        self.closed_trades += 1
        self.closed_pnl_lamports += result.pnl_lamports
        if self.keep_results:
            self.results.append((vi, tok.mint, result))
        if not (self.unlimited or var.shadow):
            self.balance[vi] += proceeds
        slot.position = None
        slot.high = None
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
        self.results_by_mint.pop(mint, None)
        self.convictions.pop(mint, None)
        for store in self.other_convictions.values():
            store.pop(mint, None)
        self.requested.discard(mint)
        self.watched.discard(mint)
        for u in self.universes:
            self.gates.pop((mint, u.name), None)
            self.late_crossed.discard((u.name, mint))
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

    def open_value(self) -> list[int]:
        """Per variant: open positions sold into today's curve after costs, plus stakes
        reserved for orders in flight. Read-only: no curve or wallet changes."""
        value = [0] * len(self.variants)
        for (vi, mint), slot in self.slots.items():
            if slot.done:
                continue
            if slot.pending is not None:
                value[vi] += slot.pending.reserved
            if slot.position is not None:
                _, gross = self._curve(slot, self.tokens[mint]).after_sell(slot.position.tokens)
                value[vi] += _proceeds(gross, self.variants[vi].fill)
        return value

    def summary(self) -> dict:
        open_value = self.open_value()
        return {
            "first_event_ms": self.first_ts,
            "last_event_ms": self.clock,
            "stats": dict(self.stats),
            "variants": len(self.variants),
            "closed_trades": self.closed_trades,
            # Cash in each paper wallet; open positions are in open_value_lamports.
            "final_balance_lamports": {
                v.name: self.balance[i] for i, v in enumerate(self.variants)
            },
            "open_value_lamports": {v.name: open_value[i] for i, v in enumerate(self.variants)},
            "equity_lamports": {
                v.name: self.balance[i] + open_value[i] for i, v in enumerate(self.variants)
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
    tables = (
        ("create", "create"),
        ("trade", "trade"),
        ("lookup", "lookups"),
        ("tracked", "tracked"),
        ("creators", "creators"),
        ("social", "social"),
    )
    for kind, table in tables:
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
        elif kind == "tracked":
            assert isinstance(item, dict)
            engine.on_tracked(item)
        elif kind == "creators":
            assert isinstance(item, dict)
            engine.on_creators(item)
        elif kind == "social":
            assert isinstance(item, dict)
            engine.on_social(item)
        elif kind == "create":
            assert isinstance(item, dict)
            engine.on_create(item)
        else:
            assert isinstance(item, dict)
            engine.on_trade(item)
    engine.finish()


class _FileFeed:
    """Loads a nightly file when it changes and records each load with the run's events."""

    table = ""
    poll_ms = TRACKED_POLL_MS

    def __init__(self, engine: Engine, sink: Sink, path: Path) -> None:
        self.engine, self.sink, self.path = engine, sink, path
        self.mtime_ms: int | None = None
        self.checked_ms: int | None = None

    def poll(self, ts: int) -> None:
        if self.checked_ms is not None and ts - self.checked_ms < self.poll_ms:
            return
        self.checked_ms = ts
        try:
            mtime = int(self.path.stat().st_mtime * 1000)
        except FileNotFoundError:
            return
        if mtime == self.mtime_ms:
            return
        fields = self.read()
        if fields is None:
            return
        self.mtime_ms = mtime
        row = {"seq": next_seq(), "received_ms": ts, "event_index": 0, "list_mtime_ms": mtime}
        row |= fields
        self.sink.add(self.table, [row])
        self.apply(row)

    def read(self) -> dict | None:
        raise NotImplementedError

    def apply(self, row: dict) -> None:
        raise NotImplementedError


class TrackedFeed(_FileFeed):
    """The tracked-wallet list (tracked.py)."""

    table = "tracked"

    def __init__(self, engine: Engine, sink: Sink, path: Path = TRACKED_PATH) -> None:
        super().__init__(engine, sink, path)

    def read(self) -> dict | None:
        wallets = load_list(self.path)
        if wallets is None:
            return None
        return {"wallets": sorted(wallets), "top": load_top(self.path, LEADER_TOP)}

    def apply(self, row: dict) -> None:
        self.engine.on_tracked(row)
        print(f"tracked-wallet list loaded: {len(row['wallets'])} wallets", flush=True)


class SocialFeed(_FileFeed):
    """Social and attention signals (social.py's state file), checked every 5 minutes."""

    table = "social"
    poll_ms = SOCIAL_POLL_MS

    def __init__(self, engine: Engine, sink: Sink, path: Path = SOCIAL_NOW) -> None:
        super().__init__(engine, sink, path)

    def read(self) -> dict | None:
        try:
            text = self.path.read_text()
            json.loads(text)
        except (FileNotFoundError, ValueError):
            return None
        return {"state": text}

    def apply(self, row: dict) -> None:
        self.engine.on_social(row)


class CreatorFeed(_FileFeed):
    """Creator records (creators.py)."""

    table = "creators"

    def __init__(self, engine: Engine, sink: Sink, path: Path = CREATORS_PATH) -> None:
        super().__init__(engine, sink, path)

    def read(self) -> dict | None:
        recs = load_creators(self.path)
        if recs is None:
            return None
        names = sorted(recs)
        return {
            "creators": names,
            "launches": [recs[c][0] for c in names],
            "graduated": [recs[c][1] for c in names],
            "last_launch_ms": [recs[c][2] for c in names],
        }

    def apply(self, row: dict) -> None:
        self.engine.on_creators(row)
        print(f"creator records loaded: {len(row['creators'])} creators", flush=True)


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
    feeds = [TrackedFeed(engine, sink), CreatorFeed(engine, sink), SocialFeed(engine, sink)]
    SOCIAL_WATCH.parent.mkdir(parents=True, exist_ok=True)
    engine.watch_path = SOCIAL_WATCH
    for feed in feeds:
        feed.poll(now_ms())

    def on_rows(creates: list[dict], trades: list[dict]) -> None:
        for feed in feeds:
            feed.poll(now_ms())
        engine.on_rows(creates, trades)

    async def stream() -> None:
        task = asyncio.create_task(worker.run())
        recording = asyncio.create_task(record(minutes, url, sink, on_rows, engine.on_gap))
        try:
            # The lookup worker never returns on its own: if it does (an exception), stop
            # the run (systemd starts a new one) rather than trade on with every candidate
            # refused as "busy".
            done, _ = await asyncio.wait({task, recording}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                raise RuntimeError(f"lookup worker stopped: {task.exception()!r}")
            recording.result()
        finally:
            for t in (task, recording):
                t.cancel()

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
    engine.keep_results = False  # runs for days: closed trades are on disk, not in memory
    if args.mode == "live":
        run_live(engine, args.minutes, source)
    else:
        run_replay(engine, source)
    engine.summary_extra["running"] = False
    engine.write_summary()
    pnl, n = engine.closed_pnl_lamports, engine.closed_trades
    print(f"{run_dir}: {n} closed paper trades, {to_sol(pnl):+.4f} SOL in total")


if __name__ == "__main__":
    main()
