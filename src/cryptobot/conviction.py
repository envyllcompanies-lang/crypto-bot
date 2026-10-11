"""Conviction: a 0-100 score for how much a token deserves an entry, and how early (M6).

The owner wants the bot to be selective: enter strong coins at once and weaker ones only
on a dip. This module turns what the engine already knows about a token (features.py)
and its once-per-token lookup (holders.py, with the LLM narrative from narrative.py)
into a score, a tier and the entry drop that tier asks for.

Every constant here is a first proposal, to be tuned on paper results, not a finding.
The score is computed only from the token's state and the lookup row recorded before the
event being decided, so live runs and replays give the same scores.

Hard blocks (no score, never trade): data tainted, graduated, older than 40 minutes,
no usable lookup yet (not done, or it failed), or top-5 funding clustered. Mayhem and
non-SOL curves are never tracked at all.

Points (summed, clamped to 0-100; the parts add up to 95 at most). The weights follow
the research report (reports/Pumpfun narratives and bot latency.md): structural signals
with published evidence weigh most, and the LLM narrative is logged but not scored.

- not a copycat: 20. Originals graduate 9.20% of the time against 0.86% for copycats
  (arXiv 2609.10246, 15.2M coins); a copycat also takes the red-flag penalty;
- creator record (creators.py): 15 if an earlier launch of theirs graduated; -10 if they
  launched another token in the previous 7 days (serial launcher); 0 if unknown;
- early buyers: 12 if the first 10 buyers (dev excluded) hold at most 15% of supply,
  falling linearly to 0 at 40% (they hold far more in high-risk tokens, MELT);
- climb efficiency: 8 at 5 or fewer trades per percentage point of curve progress,
  falling to 0 at 25 (graduates take fewer trades, arXiv 2602.14860);
- dev share: 8 up to 5%, falling linearly to 0 at 15%;
- insider share: 8 up to 20%, falling linearly to 0 at 40%;
- top-5 SOL balances not alike: 8;
- no bundle launch candle: 6;
- pro traders: 6 at 10 or more, pro rata below;
- X link: community 4, tweet 1, profile or none 0, not evaluated 1 (no outcome data);
- minus 10 per red flag (copycat).

The LLM narrative score and its red flags are recorded as "shadow" fields and change
nothing: no published evidence shows narrative themes predict outcomes, and the score
must first beat the on-chain features on later, held-out data (M4).

Thresholds inside each component are first proposals, to be tuned on recorded data.

Tiers: 80+ very_high, buy now (drop 0); 65-80 good, buy a 25% drop from the high;
50-65 average, buy a 40% drop; below 50 skip. Tiers apply to the score rounded to a
whole number, the one logs and reports show.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptobot.features import TokenState
from cryptobot.holders import balances_alike, funding_clustered

MAX_AGE_MS = 40 * 60 * 1000

NOT_COPYCAT = 20
CREATOR_GRADUATE, CREATOR_SERIAL = 15, -10
EARLY_POINTS, EARLY_FULL, EARLY_ZERO = 12, 0.15, 0.40
CLIMB_POINTS, CLIMB_FULL, CLIMB_ZERO = 8, 5.0, 25.0
DEV_POINTS, DEV_FULL, DEV_ZERO = 8, 0.05, 0.15
INSIDER_POINTS, INSIDER_FULL, INSIDER_ZERO = 8, 0.20, 0.40
HOLDERS_DIFFER = 8
NO_BUNDLE_CANDLE = 6
PRO_POINTS, PRO_FULL = 6, 10
X_POINTS = {"community": 4, "tweet": 1, "profile": 0, "none": 0, None: 1}
RED_FLAG_PENALTY = 10

# (minimum score, tier, drop from the all-time high to buy at), best first.
TIERS = ((80, "very_high", 0.0), (65, "good", 0.25), (50, "average", 0.40))
SKIP = "skip"


@dataclass(frozen=True)
class Conviction:
    score: int
    tier: str  # very_high, good, average or skip
    drop: float | None  # entry drop for the tier; None = skip
    reasons: dict[str, float] = field(default_factory=dict)  # points per component
    red_flags: tuple[str, ...] = ()
    shadow: dict = field(default_factory=dict)  # logged, never scored (the LLM narrative)

    def describe(self) -> dict:
        return {
            "score": self.score,
            "tier": self.tier,
            "drop": self.drop,
            "reasons": self.reasons,
            "red_flags": list(self.red_flags),
            "shadow": self.shadow,
        }


def _linear(value: float, full: float, zero: float, points: float) -> float:
    """`points` at or below `full`, 0 at or above `zero`, a straight line between."""
    if value <= full:
        return points
    if value >= zero:
        return 0.0
    return points * (zero - value) / (zero - full)


def blocks(tok: TokenState, result: dict | None, lookup_ms: int | None, now_ms: int) -> list[str]:
    """Reasons the token may never be traded on conviction; empty if none."""
    out = []
    if tok.tainted:
        out.append("tainted")
    if tok.curve.graduated:
        out.append("graduated")
    if tok.age_ms(now_ms) > MAX_AGE_MS:
        out.append("older_than_40m")
    if result is None or lookup_ms is None:
        out.append("no_lookup")
    elif funding_clustered(result["funders"], result["first_ms"], lookup_ms):
        out.append("funding_clustered")
    return out


def conviction(
    tok: TokenState, result: dict | None, lookup_ms: int | None, now_ms: int
) -> Conviction | None:
    """The token's conviction now, or None if a hard block applies.

    `result` is the lookup's parsed result (None if not done or failed) and `lookup_ms`
    the time it arrived, which is when its funding times were judged fresh or not.
    """
    if blocks(tok, result, lookup_ms, now_ms):
        return None
    assert result is not None
    flags = {"copycat"} if tok.copycat else set()
    creator = 0.0
    if tok.creator_graduates:
        creator += CREATOR_GRADUATE
    if tok.creator_recent_launch:
        creator += CREATOR_SERIAL
    reasons = {
        "not_copycat": 0 if tok.copycat else NOT_COPYCAT,
        "creator_record": creator,
        "early_buyers": _linear(tok.early_buyer_share, EARLY_FULL, EARLY_ZERO, EARLY_POINTS),
        "climb": _linear(tok.trades_per_progress_pct, CLIMB_FULL, CLIMB_ZERO, CLIMB_POINTS),
        "dev_share": _linear(tok.dev_share, DEV_FULL, DEV_ZERO, DEV_POINTS),
        "insider_share": _linear(tok.insider_share, INSIDER_FULL, INSIDER_ZERO, INSIDER_POINTS),
        "holders_differ": 0 if balances_alike(result["balances"]) else HOLDERS_DIFFER,
        "no_bundle_candle": 0 if tok.bundle_candle else NO_BUNDLE_CANDLE,
        "pro_traders": PRO_POINTS * min(tok.pro_traders / PRO_FULL, 1.0),
        "x_link": X_POINTS.get(result.get("x_kind"), 0),
        "red_flags": -RED_FLAG_PENALTY * len(flags),
    }
    narrative = result.get("narrative")
    shadow = {
        "narrative_score": narrative.get("score") if narrative else None,
        "narrative_theme": narrative.get("theme") if narrative else None,
        "narrative_red_flags": list(narrative.get("red_flags", ())) if narrative else [],
    }
    reasons = {k: round(v, 2) for k, v in reasons.items()}
    score = round(min(max(sum(reasons.values()), 0), 100))
    tier, drop = SKIP, None
    for minimum, name, tier_drop in TIERS:
        if score >= minimum:
            tier, drop = name, tier_drop
            break
    return Conviction(score, tier, drop, reasons, tuple(sorted(flags)), shadow)
