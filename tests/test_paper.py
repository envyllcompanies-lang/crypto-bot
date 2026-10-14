import pytest

from cryptobot.curve import (
    INITIAL_VIRTUAL_SOL,
    INITIAL_VIRTUAL_TOKENS,
    TOTAL_SUPPLY,
    Curve,
    to_lamports,
)
from cryptobot.paper import OUT_SCHEMAS, Engine, read_table, recorded_events, run_replay
from cryptobot.recorder import Sink
from cryptobot.replay import FillConfig, Trade, replay
from cryptobot.strategies import DipBuyer
from cryptobot.v1 import ALL, Variant

MINT = "Mint1111"


def create_row(
    mint=MINT, ts=0, slot=100, creator="dev", mayhem=False, seq=0, name=None, symbol=None
):
    return {
        "name": name,
        "symbol": symbol,
        "seq": seq,
        "mint": mint,
        "creator": creator,
        "user": creator,
        "received_ms": ts,
        "slot": slot,
        "virtual_sol_reserves": INITIAL_VIRTUAL_SOL,
        "virtual_token_reserves": INITIAL_VIRTUAL_TOKENS,
        "token_total_supply": TOTAL_SUPPLY,
        "is_mayhem_mode": mayhem,
        "event_index": 0,
    }


class Tape:
    """One token's trades, both as recorder rows and as replay.Trade inputs."""

    def __init__(self, mint=MINT):
        self.mint, self.curve, self.rows, self.trades = mint, Curve(), [], []
        self.held: dict[str, int] = {}

    def buy(self, ts, sol, user="buyer", slot=200):
        curve, tokens, spent = self.curve.after_buy(to_lamports(sol))
        self._row(ts, user, slot, True, spent, tokens, curve)
        self.trades.append(Trade(ts, True, sol_lamports=spent, wallet=user))
        self.held[user] = self.held.get(user, 0) + tokens

    def sell(self, ts, user, share=1.0, slot=200):
        tokens = int(self.held[user] * share)
        curve, out = self.curve.after_sell(tokens)
        self._row(ts, user, slot, False, out, tokens, curve)
        self.trades.append(Trade(ts, False, token_raw=tokens, wallet=user))
        self.held[user] -= tokens

    def _row(self, ts, user, slot, is_buy, sol, tokens, curve):
        self.rows.append(
            {
                "seq": len(self.rows) + 1,
                "mint": self.mint,
                "received_ms": ts,
                "slot": slot,
                "user": user,
                "is_buy": is_buy,
                "sol_amount": sol,
                "token_amount": tokens,
                "virtual_sol_reserves": curve.virtual_sol,
                "virtual_token_reserves": curve.virtual_tokens,
                "event_index": 0,
            }
        )
        self.curve = curve


def bounce_tape():
    """Run up, a whale dumps (a ~45% drop), then buyers push it back up."""
    t = Tape()
    t.buy(1_000, 10.0, user="whale")
    t.buy(2_000, 0.5, user="a")
    t.sell(3_000, "whale")
    for i in range(12):
        t.buy(6_000 + i * 1_000, 1.0, user=f"b{i}")
    return t


def crash_tape():
    """Run up, a dump, a small bounce, then a second dump through the entry."""
    t = Tape()
    t.buy(1_000, 10.0, user="whale")
    t.buy(1_500, 2.0, user="w2")
    t.sell(3_000, "whale")
    t.buy(6_000, 0.3, user="c")
    t.sell(9_000, "w2")
    t.buy(12_000, 0.1, user="d")
    return t


def graduation_tape():
    """Buys that carry the curve to graduation while a position is open."""
    t = Tape()
    t.buy(1_000, 2.0, user="a")
    t.sell(2_000, "a", share=0.9)
    for i in range(5):
        t.buy(5_000 + i * 1_000, 20.0, user=f"g{i}")
    return t


def open_tape():
    """The data ends with our order still in flight."""
    t = Tape()
    t.buy(1_000, 10.0, user="whale")
    t.sell(2_000, "whale")
    return t


def quiet_tape():
    """A dump, our entry, then nobody trades for a minute before one last small buy."""
    t = Tape()
    t.buy(1_000, 10.0, user="whale")
    t.sell(2_000, "whale")
    t.buy(3_000, 0.2, user="a")
    t.buy(63_000, 0.1, user="b")
    return t


TAPES = {
    "quiet": quiet_tape,
    "bounce": bounce_tape,
    "crash": crash_tape,
    "graduation": graduation_tape,
    "open": open_tape,
}
STRATEGIES = {
    "v1_core": DipBuyer(to_lamports(0.25), drop=0.40, target=0.45, max_age_ms=None),
    "dip25_2x_sl20": DipBuyer(to_lamports(0.4), drop=0.25, target=1.0, stop_tolerance=0.20),
    "no_dip_stall": DipBuyer(to_lamports(1.0), drop=0.0, target=0.5, stall_ms=3_000),
    "stall_on_clock": DipBuyer(to_lamports(0.25), drop=0.40, target=5.0, stall_ms=20_000),
    "quiet_exit": DipBuyer(to_lamports(0.25), drop=0.40, target=5.0, quiet_ms=15_000),
    "trail_no_cap": DipBuyer(
        to_lamports(0.25), drop=0.0, target=None, stop_tolerance=0.20, trail=0.10
    ),
}


def engine_results(tape, strategy, cfg):
    eng = Engine([Variant("v", ALL, strategy, cfg)], unlimited_balance=True)
    eng.on_create(create_row())
    for row in tape.rows:
        eng.on_trade(row)
    eng.finish()
    return [r for _, _, r in eng.results]


@pytest.mark.parametrize("tape", sorted(TAPES))
@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
@pytest.mark.parametrize("delay_ms", [0, 1_000, 2_000, 5_000])
def test_paper_engine_matches_replay(tape, strategy, delay_ms):
    t, s = TAPES[tape](), STRATEGIES[strategy]
    cfg = FillConfig(delay_ms=delay_ms, network_lamports_per_tx=50_000, rent_refunded=False)
    expected = replay(t.trades, s, cfg, created_ts_ms=0)
    assert engine_results(t, s, cfg) == expected


def test_parity_scenarios_are_not_vacuous():
    cfg = FillConfig(delay_ms=1_000)
    core = STRATEGIES["v1_core"]
    exits = {n: [r.exit_reason for r in replay(TAPES[n]().trades, core, cfg, 0)] for n in TAPES}
    assert exits["bounce"] == ["target"]
    assert exits["crash"] == ["stop"]
    assert exits["open"] == ["open_at_end"]
    hold = DipBuyer(to_lamports(0.5), drop=0.0, target=50.0, stop_tolerance=0.9)
    grad = replay(TAPES["graduation"]().trades, hold, cfg, 0)
    assert [r.flags for r in grad] == [("graduated",)]
    assert engine_results(TAPES["graduation"](), hold, cfg) == grad
    # Time-based exits fire on the clock while nobody trades the token.
    for name, reason in (("stall_on_clock", "stall"), ("quiet_exit", "quiet")):
        [r] = replay(TAPES["quiet"]().trades, STRATEGIES[name], cfg, 0)
        assert r.exit_reason == reason and r.exit_decided_ts_ms < 63_000


def test_clock_exits_keep_firing_after_the_last_trade_until_the_data_ends():
    t, s, cfg = open_tape(), STRATEGIES["quiet_exit"], FillConfig(delay_ms=1_000)
    assert [r.exit_reason for r in replay(t.trades, s, cfg, 0)] == ["open_at_end"]
    [r] = replay(t.trades, s, cfg, 0, end_ms=60_000)
    assert r.exit_reason == "quiet" and r.exit_decided_ts_ms == 17_000

    # In the paper engine, another token's trades move the clock on.
    eng = Engine([Variant("v", ALL, s, cfg)], unlimited_balance=True)
    eng.on_create(create_row())
    for row in t.rows:
        eng.on_trade(row)
    eng.on_create(create_row(mint="Other", ts=59_000))
    eng.on_trade({**t.rows[0], "mint": "Other", "received_ms": 60_000})
    eng.finish()
    assert [(r.exit_reason, r.exit_decided_ts_ms) for _, _, r in eng.results] == [("quiet", 17_000)]


def test_wallet_too_small_skips_entry(tmp_path):
    sink = Sink(tmp_path, OUT_SCHEMAS)
    var = Variant("v", ALL, STRATEGIES["v1_core"], FillConfig(delay_ms=1_000))
    eng = Engine([var], out=sink, start_balance=to_lamports(0.1))
    eng.on_create(create_row())
    for row in bounce_tape().rows:
        eng.on_trade(row)
    eng.finish()
    assert eng.results == []
    kinds = read_table(tmp_path, "decisions")["kind"].to_list()
    assert "skip_balance" in kinds and "fill_buy" not in kinds


def test_wallet_balance_tracks_cost_and_proceeds():
    var = Variant("v", ALL, STRATEGIES["v1_core"], FillConfig(delay_ms=1_000))
    eng = Engine([var], start_balance=to_lamports(1.0))
    eng.on_create(create_row())
    for row in bounce_tape().rows:
        eng.on_trade(row)
    eng.finish()
    ((_, _, result),) = eng.results
    assert eng.balance[0] == to_lamports(1.0) + result.pnl_lamports


def test_gap_flags_open_position_and_taints_tokens():
    t = bounce_tape()
    var = Variant("v", ALL, STRATEGIES["v1_core"], FillConfig(delay_ms=1_000))
    eng = Engine([var], unlimited_balance=True)
    eng.on_create(create_row())
    for row in t.rows[:5]:
        eng.on_trade(row)
    eng.on_gap(t.rows[4]["received_ms"] + 1)
    for row in t.rows[5:]:
        eng.on_trade(row)
    eng.finish()
    ((_, _, result),) = eng.results
    assert "gap" in result.flags
    assert eng.tokens[MINT].tainted == "gap"


def test_missed_trade_taints_token():
    t = bounce_tape()
    eng = Engine([Variant("v", ALL, STRATEGIES["v1_core"], FillConfig())], unlimited_balance=True)
    eng.on_create(create_row())
    eng.on_trade(t.rows[0])
    eng.on_trade(t.rows[2])  # row 1 never arrives
    assert eng.tokens[MINT].tainted == "missed_trade"
    assert eng.stats["tainted_missed_trade"] == 1


def test_largest_holder_selling_triggers_exit():
    t = Tape()
    t.buy(1_000, 3.0, user="whale")
    t.buy(2_000, 0.1, user="a")  # no-dip entry decided here, lands at 3 s
    t.buy(4_000, 0.1, user="b")
    t.sell(5_000, "whale", share=0.02)  # small: price stays above the -20% stop
    t.buy(9_000, 0.1, user="c")
    strategy = DipBuyer(to_lamports(0.25), drop=0.0, target=5.0, stop_tolerance=0.20)
    var = Variant("v", ALL, strategy, FillConfig(delay_ms=1_000), holder_sell=True)
    eng = Engine([var], unlimited_balance=True)
    eng.on_create(create_row())
    for row in t.rows:
        eng.on_trade(row)
    eng.finish()
    ((_, _, result),) = eng.results
    assert result.exit_reason == "largest holder sold"


def test_mayhem_and_unseen_tokens_are_ignored():
    eng = Engine([Variant("v", ALL, STRATEGIES["v1_core"], FillConfig())], unlimited_balance=True)
    eng.on_create(create_row(mayhem=True))
    for row in bounce_tape().rows:
        eng.on_trade(row)
    eng.finish()
    assert eng.results == [] and eng.stats["trades_on_tracked_tokens"] == 0


def test_on_rows_orders_a_transaction_by_event_index():
    t = Tape()
    t.buy(0, 1.0, user="dev", slot=100)
    trade = dict(t.rows[0], event_index=1)
    eng = Engine([Variant("v", ALL, STRATEGIES["v1_core"], FillConfig())], unlimited_balance=True)
    eng.on_rows([create_row()], [trade])
    assert eng.tokens[MINT].dev_share > 0


def test_recorded_events_replay_like_live(tmp_path):
    """Rows written by the recorder replay to the same trades, gaps included."""
    t = bounce_tape()
    events = Sink(tmp_path)
    events.add("create", [create_row()])
    events.add("trade", t.rows)
    gap_at = t.rows[6]["received_ms"] + 1
    session = {"txs": 8, "failed_txs": 0, "truncated_txs": 0}
    events.add(
        "sessions",
        [
            {**session, "connected_ms": 0, "disconnected_ms": gap_at, "end_reason": "Closed"},
            {
                **session,
                "connected_ms": gap_at + 5,
                "disconnected_ms": 99_000,
                "end_reason": "deadline",
            },
        ],
    )
    events.flush()
    assert [k for k, _ in recorded_events(tmp_path)].count("gap") == 1

    var = Variant("v", ALL, STRATEGIES["v1_core"], FillConfig(delay_ms=1_000))
    live = Engine([var], unlimited_balance=True)
    live.on_create(create_row())
    for row in t.rows:
        if row["received_ms"] > gap_at and live.stats["gaps"] == 0:
            live.on_gap(gap_at)
        live.on_trade(row)
    live.finish()

    out = tmp_path / "paper"
    replayed = Engine([var], out=Sink(out, OUT_SCHEMAS), unlimited_balance=True)
    run_replay(replayed, tmp_path)
    assert [r for _, _, r in replayed.results] == [r for _, _, r in live.results]
    written = read_table(out, "trades")
    assert written.height == len(live.results)
    assert written["pnl_lamports"].to_list() == [r.pnl_lamports for _, _, r in live.results]


def test_final_stretch_waits_for_a_complete_column(tmp_path):
    import json

    from cryptobot.v1 import full_grid

    stretch = [v for v in full_grid() if v.universe.name == "final_stretch_50"][:1]
    eng = Engine(stretch, out=Sink(tmp_path, OUT_SCHEMAS))
    eng.on_create(create_row())
    for row in bounce_tape().rows:
        eng.on_trade(row)
    eng.finish()
    first = json.loads(read_table(tmp_path, "gates")["checks"][0])
    assert first["column_complete"] is False


def test_summary_is_rewritten_on_flush(tmp_path):
    import json

    eng = Engine([Variant("v", ALL, STRATEGIES["v1_core"], FillConfig())], unlimited_balance=True)
    eng.summary_path = tmp_path / "summary.json"
    eng.summary_extra = {"mode": "live", "running": True}
    eng.on_create(create_row())
    rows = bounce_tape().rows
    for row in rows:
        eng.on_trade(row)
    eng.flush()
    data = json.loads(eng.summary_path.read_text())
    assert data["running"] is True
    assert data["stats"]["trades_on_tracked_tokens"] == len(rows)


def test_report_renders_a_replayed_run(tmp_path):
    import json

    from cryptobot.m6_report import lag_by_hour, report

    events = tmp_path / "events"
    sink = Sink(events)
    sink.add("create", [create_row()])
    rows = [dict(r, timestamp=r["received_ms"] // 1000 - 1) for r in bounce_tape().rows]
    sink.add("trade", rows)
    sink.flush()
    lag = lag_by_hour(events)
    assert lag["trades"].sum() == len(rows) and lag["median"][0] == 1.0

    run = tmp_path / "run"
    run.mkdir()
    var = Variant("v", ALL, STRATEGIES["v1_core"], FillConfig(delay_ms=1_000))
    (run / "variants.json").write_text(json.dumps([var.describe()]))
    eng = Engine([var], out=Sink(run, OUT_SCHEMAS), unlimited_balance=True)
    eng.summary_path = run / "summary.json"
    eng.summary_extra = {"mode": "replay", "source": str(events), "running": False}
    run_replay(eng, events)
    text = "\n".join(report(run))
    assert "| Status | finished |" in text
    assert "## Feed lag" in text and "| 1.00 s |" in text
    assert "| all | v | 1 | 100.0%" in text


def test_lookup_delays_split_holder_lookups_from_narrative_verdicts(tmp_path):
    import json

    from cryptobot.holders import LOOKUP_SCHEMA
    from cryptobot.m6_report import lookup_delays, lookup_section

    def row(seq, received, result, error=None):
        return {
            "seq": seq,
            "received_ms": received,
            "event_index": 0,
            "mint": "m",
            "requested_ms": 1_000,
            "result": None if result is None else json.dumps(result),
            "error": error,
        }

    sink = Sink(tmp_path, {"lookups": LOOKUP_SCHEMA})
    sink.add(
        "lookups",
        [
            row(1, 2_000, {"wallets": []}),
            row(2, 4_000, None, error="timeout"),
            row(3, 9_000, {"narrative_update": True, "narrative": {"score": 3}}),
        ],
    )
    sink.flush()
    table = {r["kind"]: r for r in lookup_delays(tmp_path).iter_rows(named=True)}
    assert table["holders"]["n"] == 2 and table["holders"]["failed"] == 1
    assert table["holders"]["worst"] == 3.0
    assert table["narrative"]["n"] == 1 and table["narrative"]["median"] == 8.0
    text = "\n".join(lookup_section(tmp_path))
    assert "| narrative | 1 | 0 | 8.00 s |" in text
    assert "No lookups" in "\n".join(lookup_section(tmp_path / "missing"))


def late_tape(leader="leader"):
    """Buyers take the curve past 90% (the 12th 5-SOL buy), then a holder sells."""
    t = Tape()
    t.buy(1_000, 5, user=leader)
    for k in range(1, 14):
        t.buy(1_000 + k * 3_000, 5, user=f"b{k}")
    t.sell(60_000, "b1")
    return t


def test_late_universe_buys_at_the_first_cross_only_after_a_top_leader(tmp_path):
    from cryptobot.v1 import late_variants

    for top, want in ((["leader"], {"late_p90_leader", "late_p90"}), (["other"], {"late_p90"})):
        eng = Engine(late_variants(), out=Sink(tmp_path / top[0], OUT_SCHEMAS))
        eng.on_tracked({"received_ms": 0, "wallets": ["leader", "other"], "top": top})
        eng.on_create(create_row())
        for row in late_tape().rows:
            eng.on_trade(row)
        eng.finish()
        buys = read_table(tmp_path / top[0], "decisions").filter(
            read_table(tmp_path / top[0], "decisions")["kind"] == "signal_buy"
        )
        bought = {v.split("|")[0] for v in buys["variant"].to_list()}
        assert bought == want
        # one decision per variant, made on the trade that first crossed 90%
        assert buys.height == 2 * len(want)
        assert set(buys["ts_ms"].to_list()) == {1_000 + 11 * 3_000}


def test_late_leader_universe_waits_for_a_leader_list(tmp_path):
    from cryptobot.v1 import late_variants

    eng = Engine(late_variants(), out=Sink(tmp_path, OUT_SCHEMAS))
    eng.on_create(create_row())
    for row in late_tape().rows:
        eng.on_trade(row)
    eng.finish()
    d = read_table(tmp_path, "decisions")
    bought = {v.split("|")[0] for v in d.filter(d["kind"] == "signal_buy")["variant"].to_list()}
    assert bought == {"late_p90"}
