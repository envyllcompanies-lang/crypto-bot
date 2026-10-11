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

Points (summed, clamped to 0-100; the parts add up to 96 at most):
- narrative score 1-5 from the LLM: 0, 10, 20, 30 or 40; not evaluated: a neutral 15;
- X link: community 12, tweet 4, profile 2, none 0, not evaluated 4;
- top-5 SOL balances not alike: 8;
- dev share: 8 up to 5%, falling linearly to 0 at 15%;
- insider share: 8 up to 20%, falling linearly to 0 at 40%;
- pro traders: 8 at 10 or more, pro rata below;
- not a copycat: 8 (a copycat also counts as a red flag);
- no bundle launch candle: 4;
- minus 10 per red flag (the LLM's, plus copycat).

Tiers: 80+ very_high, buy now (drop 0); 65-80 good, buy a 25% drop from the high;
50-65 average, buy a 40% drop; below 50 skip. Tiers apply to the score rounded to a
whole number, the one logs and reports show.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptobot.features import TokenState
from cryptobot.holders import balances_alike, funding_clustered

MAX_AGE_MS = 40 * 60 * 1000

NARRATIVE_POINTS = 10  # per narrative score step above 1, so 5/5 is worth 40
NARRATIVE_NOT_EVALUATED = 15
X_POINTS = {"community": 12, "tweet": 4, "profile": 2, "none": 0, None: 4}
HOLDERS_DIFFER = 8
DEV_POINTS, DEV_FULL, DEV_ZERO = 8, 0.05, 0.15
INSIDER_POINTS, INSIDER_FULL, INSIDER_ZERO = 8, 0.20, 0.40
PRO_POINTS, PRO_FULL = 8, 10
NOT_COPYCAT = 8
NO_BUNDLE_CANDLE = 4
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

    def describe(self) -> dict:
        return {
            "score": self.score,
            "tier": self.tier,
            "drop": self.drop,
            "reasons": self.reasons,
            "red_flags": list(self.red_flags),
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
    narrative = result.get("narrative")
    flags = set(narrative["red_flags"]) if narrative else set()
    if tok.copycat:
        flags.add("copycat")
    reasons = {
        "narrative": (
            (narrative["score"] - 1) * NARRATIVE_POINTS if narrative else NARRATIVE_NOT_EVALUATED
        ),
        "x_link": X_POINTS.get(result.get("x_kind"), 0),
        "holders_differ": 0 if balances_alike(result["balances"]) else HOLDERS_DIFFER,
        "dev_share": _linear(tok.dev_share, DEV_FULL, DEV_ZERO, DEV_POINTS),
        "insider_share": _linear(tok.insider_share, INSIDER_FULL, INSIDER_ZERO, INSIDER_POINTS),
        "pro_traders": PRO_POINTS * min(tok.pro_traders / PRO_FULL, 1.0),
        "not_copycat": 0 if tok.copycat else NOT_COPYCAT,
        "no_bundle_candle": 0 if tok.bundle_candle else NO_BUNDLE_CANDLE,
        "red_flags": -RED_FLAG_PENALTY * len(flags),
    }
    reasons = {k: round(v, 2) for k, v in reasons.items()}
    score = round(min(max(sum(reasons.values()), 0), 100))
    tier, drop = SKIP, None
    for minimum, name, tier_drop in TIERS:
        if score >= minimum:
            tier, drop = name, tier_drop
            break
    return Conviction(score, tier, drop, reasons, tuple(sorted(flags)))
