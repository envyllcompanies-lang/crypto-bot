"""V1 baseline rules (docs/research/source-videos.md) as paper-trading variants (M6).

Dollar levels become multiples of launch market cap at the SOL price V1 implies: V1
says tokens launch at "$4-5K" and the curve fixes launch at 27.96 SOL, so the midpoint,
$4.5K, stands for the video's SOL price. Final Stretch is Terminal's "Soon" column; its
entry threshold isn't stated, so each curve-progress cut-off is its own universe.

V1's X-link and top-5 holder checks need network lookups (holders.py); the paper engine
runs them live once per candidate and records the answers. "Few tracked wallets" counts
holders on the list tracked.py builds from recorded trades; with no list yet it is
recorded as None (not evaluated), never as passed, and reports say so.

Beyond V1: the "conviction" universe (the owner's idea, not V1's) takes the same young
tokens as New Pairs and decides how early to enter from each token's conviction tier
(conviction.py): at once when conviction is very high, on a 25% or 40% dip when it is
good or average, never otherwise. Its exits are V1's.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptobot.costs import DIRECT, TERMINAL_REFERRAL, Venue
from cryptobot.curve import implied_sol_price, to_lamports, usd_to_launch_multiple
from cryptobot.features import TokenState
from cryptobot.replay import FillConfig, Strategy
from cryptobot.strategies import DipBuyer

V1_LAUNCH_USD = 4_500
V1_SOL_USD = implied_sol_price(V1_LAUNCH_USD)
NEW_PAIRS_MIN_MULT = usd_to_launch_multiple(7_000, V1_SOL_USD)
FINAL_STRETCH_MIN_MULT = usd_to_launch_multiple(8_000, V1_SOL_USD)
FINAL_STRETCH_MAX_AGE_MS = 40 * 60 * 1000
# Not stated in V1: assume New Pairs shows tokens up to the same age as Final Stretch.
NEW_PAIRS_MAX_AGE_MS = FINAL_STRETCH_MAX_AGE_MS
DEV_MAX_SHARE = 0.05
INSIDERS_MAX_SHARE = 0.20
PRO_TRADERS_MIN = 10
# Curve progress at which a token joins Final Stretch; V1's examples sit at 87-100%.
FINAL_STRETCH_CUTOFFS = (0.50, 0.75, 0.87)
# V1 looks at the bottom four of Final Stretch by market cap and skips the top two.
COLUMN_SKIP_TOP = 2
COLUMN_LOOK_BOTTOM = 4

NEW_PAIRS_STAKE = to_lamports(0.25)
FINAL_STRETCH_STAKE = to_lamports(0.4)
MAX_STAKE = to_lamports(1.0)  # no single trade above 1 SOL
HARD_MAX_STAKE = to_lamports(1.5)  # the cap may rise to 1.5 SOL later, never higher

ENTRY_DROPS = (0.0, 0.25, 0.40, 0.50)  # drop from the all-time high; 0 = no dip
TARGETS = (0.45, 0.50, 1.00)
STOP_TOLERANCES = (0.0, 0.20)  # 0 = V1's first price below entry; 0.20 = -20% stop
DEFAULT_DELAY_MS = 2_000
DELAYS_MS = (1_000, 2_000, 5_000)
STALL_MS = 10 * 60 * 1000
# Not V1's rule: V1 sells below entry, but a token nobody trades never moves, so the
# position would sit forever. Every variant sells after this long with no trade at all.
QUIET_MS = 5 * 60 * 1000
CORE = (0.40, 0.45, 0.0)  # V1's own method: 40% dip, +45% bounce, exit at entry

# Filled in by the paper engine from a live lookup (holders.py); None until then.
LOOKUP_CHECKS = ("x_link_is_community", "top5_sol_balances_differ", "top5_funding_not_clustered")
NOT_EVALUATED: tuple[str, ...] = ()
# V1 avoids tokens with "a ton of tracked wallets"; it gives no number. M4 tests others.
MAX_TRACKED_HOLDERS = 2


@dataclass(frozen=True)
class Universe:
    name: str
    kind: str  # "all" (no filter), "new_pairs", "final_stretch" or "conviction"
    stake_lamports: int
    cutoff: float = 0.0  # Final Stretch only: minimum curve progress
    narrative_gate: bool = False  # conviction only: LLM narrative score caps the tier
    control: bool = False  # conviction only: a coin flip picks tokens, not the score
    labels: bool = False  # conviction only: every eligible token, entered at once


ALL = Universe("all", "all", NEW_PAIRS_STAKE)


def universes(max_stake: int = MAX_STAKE) -> list[Universe]:
    new_pairs = Universe("new_pairs", "new_pairs", min(NEW_PAIRS_STAKE, max_stake))
    stake = min(FINAL_STRETCH_STAKE, max_stake)
    stretch = [
        Universe(f"final_stretch_{round(c * 100)}", "final_stretch", stake, c)
        for c in FINAL_STRETCH_CUTOFFS
    ]
    return [new_pairs, *stretch]


def conviction_universe(max_stake: int = MAX_STAKE, narrative_gate: bool = False) -> Universe:
    name = "conviction_narrative" if narrative_gate else "conviction"
    stake = min(NEW_PAIRS_STAKE, max_stake)
    return Universe(name, "conviction", stake, narrative_gate=narrative_gate)


def control_universe(max_stake: int = MAX_STAKE) -> Universe:
    return Universe("control_random", "conviction", min(NEW_PAIRS_STAKE, max_stake), control=True)


def labels_universe(max_stake: int = MAX_STAKE) -> Universe:
    return Universe("labels", "conviction", min(NEW_PAIRS_STAKE, max_stake), labels=True)


def check(
    u: Universe, tok: TokenState, now_ms: int, tracked: frozenset[str] | None = None
) -> dict[str, bool | None]:
    """Each V1 rule's result for this token now; None means not evaluated.

    `tracked` is the tracked-wallet list (tracked.py), or None if none is loaded.
    """
    if u.kind == "all":
        return {}
    if u.kind == "conviction":
        # Only the entry gate; the rest is scored, not filtered (conviction.py).
        return {
            "not_tainted": tok.tainted is None,
            "not_graduated": not tok.curve.graduated,
            "age_le_40m": tok.age_ms(now_ms) <= NEW_PAIRS_MAX_AGE_MS,
            "mcap_ge_7k": tok.mcap_multiple >= NEW_PAIRS_MIN_MULT,
        }
    checks: dict[str, bool | None] = {
        "not_tainted": tok.tainted is None,
        "not_graduated": not tok.curve.graduated,
        "no_bundle_candle": not tok.bundle_candle,
    }
    age = tok.age_ms(now_ms)
    if u.kind == "new_pairs":
        checks["age_le_40m"] = age <= NEW_PAIRS_MAX_AGE_MS
        checks["mcap_ge_7k"] = tok.mcap_multiple >= NEW_PAIRS_MIN_MULT
    else:
        checks["age_le_40m"] = age <= FINAL_STRETCH_MAX_AGE_MS
        checks["progress_ge_cutoff"] = tok.curve.progress >= u.cutoff
        checks["mcap_ge_8k"] = tok.mcap_multiple >= FINAL_STRETCH_MIN_MULT
        checks["dev_le_5pct"] = tok.dev_share <= DEV_MAX_SHARE
        checks["insiders_le_20pct"] = tok.insider_share <= INSIDERS_MAX_SHARE
        checks["pro_traders_ge_10"] = tok.pro_traders >= PRO_TRADERS_MIN
    checks.update(dict.fromkeys(LOOKUP_CHECKS + NOT_EVALUATED))
    checks["few_tracked_wallets"] = (
        None if tracked is None else tok.tracked_holders(tracked) <= MAX_TRACKED_HOLDERS
    )
    return checks


def passes(checks: dict[str, bool | None]) -> bool:
    """True if every evaluated rule passed. Unevaluated rules are skipped, not passed."""
    return all(v for v in checks.values() if v is not None)


def column_window(members: dict[str, tuple[float, int]], now_ms: int) -> set[str]:
    """The Final Stretch tokens V1 looks at.

    `members` maps mint -> (market cap multiple, created_ms) for tokens that pass the
    column's filters; tokens past the age cap are removed. Sorted by market cap, the
    top two are skipped and the bottom four kept.
    """
    old = [m for m, (_, made) in members.items() if now_ms - made > FINAL_STRETCH_MAX_AGE_MS]
    for mint in old:
        del members[mint]
    order = sorted(members, key=lambda m: members[m][0], reverse=True)
    return set(order[COLUMN_SKIP_TOP:][-COLUMN_LOOK_BOTTOM:])


@dataclass(frozen=True)
class Variant:
    name: str
    universe: Universe
    strategy: Strategy
    fill: FillConfig
    holder_sell: bool = False  # also exit when the largest holder at entry sells
    conviction: bool = False  # entry drop comes from the token's conviction tier
    shadow: bool = False  # no wallet limit: records outcomes, is not a paper wallet

    def __post_init__(self) -> None:
        stake = getattr(self.strategy, "stake_lamports", 0)
        if stake > HARD_MAX_STAKE:
            raise ValueError(f"{self.name}: stake {stake} lamports is above the 1.5 SOL hard cap")

    def describe(self) -> dict:
        s, f = self.strategy, self.fill
        return {
            "name": self.name,
            "universe": self.universe.name,
            "kind": self.universe.kind,
            "cutoff": self.universe.cutoff,
            "narrative_gate": self.universe.narrative_gate,
            "control": self.universe.control,
            "labels": self.universe.labels,
            "shadow": self.shadow,
            "stake_lamports": getattr(s, "stake_lamports", None),
            "drop": "conviction tier" if self.conviction else getattr(s, "drop", None),
            "target": getattr(s, "target", None),
            "stop_tolerance": getattr(s, "stop_tolerance", None),
            "stall_ms": getattr(s, "stall_ms", None),
            "trail": getattr(s, "trail", None),
            "quiet_ms": getattr(s, "quiet_ms", None),
            "holder_sell": self.holder_sell,
            "conviction": self.conviction,
            "delay_ms": f.delay_ms,
            "venue": f.venue.name,
            "fee_bps_per_side": f.venue.fee_bps_per_side,
            "network_lamports_per_tx": f.network_lamports_per_tx,
            "rent_lamports": f.rent_lamports,
            "rent_refunded": f.rent_refunded,
            "max_trades_per_token": f.max_trades_per_token,
        }


def variant(
    u: Universe,
    drop: float,
    target: float,
    stop: float,
    delay_ms: int = DEFAULT_DELAY_MS,
    stall_ms: int | None = None,
    holder_sell: bool = False,
) -> Variant:
    parts = [u.name, f"dip{round(drop * 100)}", f"tp{round(target * 100)}"]
    parts += [f"sl{round(stop * 100)}", f"d{delay_ms // 1000}s"]
    if stall_ms:
        parts.append(f"stall{stall_ms // 60_000}m")
    if holder_sell:
        parts.append("holdersell")
    strategy = DipBuyer(
        u.stake_lamports,
        drop=drop,
        target=target,
        stop_tolerance=stop,
        min_peak_multiple=1.0,
        max_age_ms=None,  # the universe applies the age cap
        stall_ms=stall_ms,
        quiet_ms=QUIET_MS,
    )
    return Variant("|".join(parts), u, strategy, FillConfig(delay_ms=delay_ms), holder_sell)


def conviction_variant(
    u: Universe, target: float, stop: float, delay_ms: int = DEFAULT_DELAY_MS, shadow=False
) -> Variant:
    """Entry drop from the conviction tier at decision time; V1's exits."""
    name = f"{u.name}|tp{round(target * 100)}|sl{round(stop * 100)}|d{delay_ms // 1000}s"
    # drop is replaced by the tier's drop on every entry decision; exits ignore it.
    strategy = DipBuyer(
        u.stake_lamports,
        drop=0.0,
        target=target,
        stop_tolerance=stop,
        min_peak_multiple=1.0,
        max_age_ms=None,
        quiet_ms=QUIET_MS,
    )
    fill = FillConfig(delay_ms=delay_ms)
    return Variant(name, u, strategy, fill, conviction=True, shadow=shadow)


# Exits the shadow labels universe measures on every eligible token: V1's +45% with its
# exit at entry, and the two best scalps of the 11 Oct backtest (+5% and +10%, -10% stop).
LABEL_EXITS = ((0.45, 0.0), (0.05, 0.10), (0.10, 0.10))


def default_variants(max_stake: int = MAX_STAKE) -> list[Variant]:
    """The grid run side by side, for New Pairs and each Final Stretch cut-off: every
    entry x target x stop at a 2 s delay, plus V1's core rule at 1 s and 5 s delays,
    with a 10-minute stall, and with the largest-holder-sells exit. Then the conviction
    universe (ungated, narrative-gated, and the random control) with every target x stop
    at a 2 s delay, and shadow labels variants with LABEL_EXITS."""
    if max_stake > HARD_MAX_STAKE:
        raise ValueError("max_stake is above the 1.5 SOL hard cap")
    out = []
    for u in universes(max_stake):
        for drop in ENTRY_DROPS:
            for target in TARGETS:
                for stop in STOP_TOLERANCES:
                    out.append(variant(u, drop, target, stop))
        out += [variant(u, *CORE, delay_ms=d) for d in DELAYS_MS if d != DEFAULT_DELAY_MS]
        out.append(variant(u, *CORE, stall_ms=STALL_MS))
        out.append(variant(u, *CORE, holder_sell=True))
    for u in (
        conviction_universe(max_stake),
        conviction_universe(max_stake, narrative_gate=True),
        control_universe(max_stake),
    ):
        out += [conviction_variant(u, t, s) for t in TARGETS for s in STOP_TOLERANCES]
    labels = labels_universe(max_stake)
    out += [conviction_variant(labels, t, s, shadow=True) for t, s in LABEL_EXITS]
    return out


# Scalping (owner, 11 Oct 2026): small targets, tight stops. Shadow variants on the labels
# universe (every eligible token, entered the moment it qualifies, no wallet limit), so
# each target and stop is measured on the same tokens; filter them afterwards by features.
SCALP_TARGETS = (0.05, 0.10, 0.15, 0.20, 0.30, 0.45)
SCALP_STOPS = (0.0, 0.05, 0.10)


def scalp_variants(max_stake: int = MAX_STAKE) -> list[Variant]:
    u = labels_universe(max_stake)
    return [conviction_variant(u, t, s, shadow=True) for t in SCALP_TARGETS for s in SCALP_STOPS]


# Letting winners run (11 Oct 2026): no profit cap, a stop below entry, and a trailing stop
# that sells once the price falls this far from its high since entry. Shadow variants on
# every eligible token, like the scalp grid.
TRAILS = (0.10, 0.20, 0.30, 0.50)
TRAIL_STOPS = (0.10, 0.20)


def trail_variant(
    u: Universe, trail: float, stop: float, shadow: bool = True, venue: Venue = TERMINAL_REFERRAL
) -> Variant:
    name = f"{u.name}|trail{round(trail * 100)}|sl{round(stop * 100)}|d{DEFAULT_DELAY_MS // 1000}s"
    if venue is not TERMINAL_REFERRAL:
        name += "|direct" if venue is DIRECT else f"|{venue.name}"
    strategy = DipBuyer(
        u.stake_lamports,
        drop=0.0,
        target=None,
        stop_tolerance=stop,
        min_peak_multiple=1.0,
        max_age_ms=None,
        quiet_ms=QUIET_MS,
        trail=trail,
    )
    fill = FillConfig(delay_ms=DEFAULT_DELAY_MS, venue=venue)
    return Variant(name, u, strategy, fill, conviction=True, shadow=shadow)


def trail_variants(max_stake: int = MAX_STAKE) -> list[Variant]:
    """Each trail and stop, through Terminal (as every other variant) and direct to the
    program (the lower fee a bot sending its own transactions would pay)."""
    u = labels_universe(max_stake)
    return [
        trail_variant(u, t, s, venue=v)
        for v in (TERMINAL_REFERRAL, DIRECT)
        for t in TRAILS
        for s in TRAIL_STOPS
    ]
