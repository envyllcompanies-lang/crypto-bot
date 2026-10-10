"""Pump.fun bonding curve: a constant product over virtual reserves.

All amounts are integers in lamports (SOL) and raw token units (6 decimals).
Convert to SOL or whole tokens only at the edges, with the helpers at the bottom.

Constants come from pump-fun/pump-public-docs (program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P). Rounding follows the program's
integer maths: the trader always gets the floor of what the curve gives out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
RAW_PER_TOKEN = 10**TOKEN_DECIMALS

INITIAL_VIRTUAL_SOL = 30 * LAMPORTS_PER_SOL
INITIAL_VIRTUAL_TOKENS = 1_073_000_000 * RAW_PER_TOKEN
CURVE_SALE_TOKENS = 793_100_000 * RAW_PER_TOKEN
TOTAL_SUPPLY = 1_000_000_000 * RAW_PER_TOKEN

# Virtual tokens left when every curve token is sold, which is graduation.
GRADUATION_VIRTUAL_TOKENS = INITIAL_VIRTUAL_TOKENS - CURVE_SALE_TOKENS


@dataclass(frozen=True)
class Curve:
    """State of one token's bonding curve."""

    virtual_sol: int = INITIAL_VIRTUAL_SOL
    virtual_tokens: int = INITIAL_VIRTUAL_TOKENS

    @property
    def k(self) -> int:
        return self.virtual_sol * self.virtual_tokens

    @property
    def real_sol(self) -> int:
        """Lamports actually held by the curve (virtual minus the 30 SOL seed)."""
        return self.virtual_sol - INITIAL_VIRTUAL_SOL

    @property
    def tokens_sold(self) -> int:
        return INITIAL_VIRTUAL_TOKENS - self.virtual_tokens

    @property
    def progress(self) -> float:
        """Share of the 793.1M curve tokens sold, 0.0 to 1.0."""
        return self.tokens_sold / CURVE_SALE_TOKENS

    @property
    def graduated(self) -> bool:
        return self.virtual_tokens <= GRADUATION_VIRTUAL_TOKENS

    def price(self) -> float:
        """Lamports per raw token unit."""
        return self.virtual_sol / self.virtual_tokens

    def market_cap_lamports(self) -> int:
        return self.virtual_sol * TOTAL_SUPPLY // self.virtual_tokens

    def tokens_out(self, sol_in: int) -> int:
        """Raw tokens received for `sol_in` lamports reaching the curve (after fees)."""
        if sol_in < 0:
            raise ValueError("sol_in must be non-negative")
        out = sol_in * self.virtual_tokens // (self.virtual_sol + sol_in)
        return min(out, self.virtual_tokens - GRADUATION_VIRTUAL_TOKENS)

    def sol_out(self, tokens_in: int) -> int:
        """Lamports the curve pays for `tokens_in` raw tokens (before fees)."""
        if tokens_in < 0:
            raise ValueError("tokens_in must be non-negative")
        return tokens_in * self.virtual_sol // (self.virtual_tokens + tokens_in)

    def after_buy(self, sol_in: int) -> tuple[Curve, int]:
        out = self.tokens_out(sol_in)
        return Curve(self.virtual_sol + sol_in, self.virtual_tokens - out), out

    def after_sell(self, tokens_in: int) -> tuple[Curve, int]:
        out = self.sol_out(tokens_in)
        return Curve(self.virtual_sol - out, self.virtual_tokens + tokens_in), out

    @classmethod
    def at_multiple(cls, multiple: float) -> Curve:
        """Curve state whose market cap is `multiple` times the launch market cap.

        Market cap is proportional to virtual_sol squared, so virtual_sol scales
        with the square root of the multiple.
        """
        if multiple < 1:
            raise ValueError("multiple must be at least 1 (launch)")
        k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS
        virtual_sol = round(INITIAL_VIRTUAL_SOL * math.sqrt(multiple))
        return cls(virtual_sol, k // virtual_sol)


LAUNCH = Curve()
LAUNCH_MARKET_CAP = LAUNCH.market_cap_lamports()


def graduation_curve() -> Curve:
    k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS
    return Curve(-(-k // GRADUATION_VIRTUAL_TOKENS), GRADUATION_VIRTUAL_TOKENS)


def launch_multiple(curve: Curve) -> float:
    return curve.market_cap_lamports() / LAUNCH_MARKET_CAP


# Edge conversions -----------------------------------------------------------


def to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


def to_lamports(sol: float) -> int:
    return round(sol * LAMPORTS_PER_SOL)


def usd_to_launch_multiple(market_cap_usd: float, sol_usd: float) -> float:
    """Convert a dollar market cap quoted at a given SOL price to a launch multiple."""
    return market_cap_usd / (to_sol(LAUNCH_MARKET_CAP) * sol_usd)


def implied_sol_price(launch_market_cap_usd: float) -> float:
    """SOL price at which a video's quoted launch market cap holds."""
    return launch_market_cap_usd / to_sol(LAUNCH_MARKET_CAP)
