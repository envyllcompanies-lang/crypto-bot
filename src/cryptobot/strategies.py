"""Strategies for the replay engine.

DipBuyer is V1's price mechanics only: buy a drop from the all-time high, take
profit on the bounce, and sell at the first price below entry. It has none of the
universe filters or bundle checks yet; those arrive with real data in M3/M4.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptobot.curve import LAUNCH
from cryptobot.replay import Buy, Sell, View

LAUNCH_PRICE = LAUNCH.price()


@dataclass(frozen=True)
class DipBuyer:
    stake_lamports: int
    drop: float = 0.40  # buy when price is this far below the all-time high
    target: float = 0.45  # take profit this far above the average entry
    stop_tolerance: float = 0.0  # V1: sell at the first price below entry
    min_peak_multiple: float = 1.0  # all-time high must be at least this x launch price
    max_age_ms: int | None = 40 * 60 * 1000
    stall_ms: int | None = None  # sell if no exit after this long
    quiet_ms: int | None = None  # sell if nobody has traded the token for this long

    def decide(self, view: View) -> Buy | Sell | None:
        pos = view.position
        if pos is None:
            if self.max_age_ms is not None and view.age_ms > self.max_age_ms:
                return None
            if view.peak_price < LAUNCH_PRICE * self.min_peak_multiple:
                return None
            if view.price <= view.peak_price * (1 - self.drop):
                return Buy(self.stake_lamports, f"drop {self.drop:.0%} from high")
            return None
        if view.price >= pos.entry_price * (1 + self.target):
            return Sell("target")
        if view.price < pos.entry_price * (1 - self.stop_tolerance):
            return Sell("stop")
        if self.stall_ms is not None and view.ts_ms - pos.entry_ts_ms >= self.stall_ms:
            return Sell("stall")
        if self.quiet_ms is not None and view.quiet_ms >= self.quiet_ms:
            return Sell("quiet")
        return None
