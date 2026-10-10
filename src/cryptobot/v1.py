"""V1 baseline rules (docs/research/source-videos.md) as paper-trading variants (M6).

Dollar levels become multiples of launch market cap at the SOL price V1 implies: V1
says tokens launch at "$4-5K" and the curve fixes launch at 27.96 SOL, so the midpoint,
$4.5K, stands for the video's SOL price. Final Stretch is Terminal's "Soon" column; its
entry threshold isn't stated, so each curve-progress cut-off is its own universe.

Rules that need data we don't have live (X link type, top-5 holders' SOL balances and
funding, tracked wallets) are recorded as None, never as passed, and the baseline runs
without them. Results are "V1 minus those checks", and reports say so.
"""

from __future__ import annotations

from dataclasses import dataclass

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
CORE = (0.40, 0.45, 0.0)  # V1's own method: 40% dip, +45% bounce, exit at entry

NOT_EVALUATED = (
    "x_link_is_community",
    "top5_sol_balances_differ",
    "top5_funding_not_clustered",
    "few_tracked_wallets",
)


@dataclass(frozen=True)
class Universe:
    name: str
    kind: str  # "all" (no filter), "new_pairs" or "final_stretch"
    stake_lamports: int
    cutoff: float = 0.0  # Final Stretch only: minimum curve progress


ALL = Universe("all", "all", NEW_PAIRS_STAKE)


def universes(max_stake: int = MAX_STAKE) -> list[Universe]:
    new_pairs = Universe("new_pairs", "new_pairs", min(NEW_PAIRS_STAKE, max_stake))
    stake = min(FINAL_STRETCH_STAKE, max_stake)
    stretch = [
        Universe(f"final_stretch_{round(c * 100)}", "final_stretch", stake, c)
        for c in FINAL_STRETCH_CUTOFFS
    ]
    return [new_pairs, *stretch]


def check(u: Universe, tok: TokenState, now_ms: int) -> dict[str, bool | None]:
    """Each V1 rule's result for this token now; None means not evaluated."""
    if u.kind == "all":
        return {}
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
    checks.update(dict.fromkeys(NOT_EVALUATED))
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
            "stake_lamports": getattr(s, "stake_lamports", None),
            "drop": getattr(s, "drop", None),
            "target": getattr(s, "target", None),
            "stop_tolerance": getattr(s, "stop_tolerance", None),
            "stall_ms": getattr(s, "stall_ms", None),
            "holder_sell": self.holder_sell,
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
    )
    return Variant("|".join(parts), u, strategy, FillConfig(delay_ms=delay_ms), holder_sell)


def default_variants(max_stake: int = MAX_STAKE) -> list[Variant]:
    """The grid run side by side, for New Pairs and each Final Stretch cut-off: every
    entry x target x stop at a 2 s delay, plus V1's core rule at 1 s and 5 s delays,
    with a 10-minute stall, and with the largest-holder-sells exit."""
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
    return out
