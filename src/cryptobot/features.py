"""Per-token live state and V1 features, built from create and trade events alone (M6).

Every feature here comes from CreateEvent and TradeEvent fields (see events.py), so it
is computed the same way live and on recorded data. Only tokens whose create event we
saw are tracked: for older tokens we would not know the age or the early holders.

Not computed here, because they need extra RPC calls or token metadata: holders' SOL
balances, funding sources, tracked wallets and X links. v1.py records them as not
evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptobot.curve import INITIAL_VIRTUAL_SOL, TOTAL_SUPPLY, Curve, launch_multiple, to_lamports

# Proxy for Terminal's "pro traders" (a platform label we cannot see): wallets that
# bought at least this much SOL of the token.
PRO_MIN_BUY_LAMPORTS = to_lamports(0.5)
# V1's bundle sign: one launch candle with no sells from $4-5K to $10-14K, about
# 2.0-3.5x launch. A token that reaches this multiple before its first sell is flagged.
BUNDLE_CANDLE_MULT = 2.0
# Wallets whose first buy lands in the create slot or the next one count as insiders
# (with the creator), the definition loopholetape/pumpfun-launches uses for bundles.
INSIDER_SLOTS = 1


def is_classic(create: dict) -> bool:
    """SOL-quoted (30 SOL virtual at create) and not in Mayhem mode."""
    return create["virtual_sol_reserves"] == INITIAL_VIRTUAL_SOL and not create["is_mayhem_mode"]


@dataclass
class TokenState:
    mint: str
    creator: str
    created_ms: int
    create_slot: int
    curve: Curve
    peak_price: float
    last_ts_ms: int
    balances: dict[str, int] = field(default_factory=dict)
    bought: dict[str, int] = field(default_factory=dict)
    insiders: set[str] = field(default_factory=set)
    pro_traders: int = 0
    n_trades: int = 0
    n_sells: int = 0
    launch_run_mult: float = 1.0  # highest multiple reached before the first sell
    tainted: str | None = None  # "missed_trade" or "gap": state may be wrong from here on
    uri: str = ""  # metadata link the creator set; text written by the creator, untrusted
    name: str = ""  # creator-written, untrusted: shown in reports and judged, never acted on
    symbol: str = ""
    copycat: bool = False  # name or symbol reused from a launch in the previous 24 hours

    @classmethod
    def from_create(cls, row: dict) -> TokenState:
        curve = Curve(row["virtual_sol_reserves"], row["virtual_token_reserves"])
        return cls(
            mint=row["mint"],
            creator=row["creator"],
            created_ms=row["received_ms"],
            create_slot=row["slot"],
            curve=curve,
            peak_price=curve.price(),
            last_ts_ms=row["received_ms"],
            uri=row.get("uri") or "",
            name=row.get("name") or "",
            symbol=row.get("symbol") or "",
        )

    def apply_trade(self, row: dict) -> None:
        """Advance to the curve this trade left and update holders.

        The event carries reserves after the trade. If the reserves before it (after
        minus the trade's amounts) don't match the last known state, a trade was
        missed and the token is marked tainted.
        """
        sol, tokens, is_buy = row["sol_amount"], row["token_amount"], row["is_buy"]
        vsol, vtok = row["virtual_sol_reserves"], row["virtual_token_reserves"]
        before = (vsol - sol, vtok + tokens) if is_buy else (vsol + sol, vtok - tokens)
        if before != (self.curve.virtual_sol, self.curve.virtual_tokens) and not self.tainted:
            self.tainted = "missed_trade"
        self.curve = Curve(vsol, vtok)
        self.peak_price = max(self.peak_price, self.curve.price())
        self.last_ts_ms = row["received_ms"]
        self.n_trades += 1
        user = row["user"]
        if is_buy:
            self.balances[user] = self.balances.get(user, 0) + tokens
            was = self.bought.get(user, 0)
            self.bought[user] = was + sol
            if was < PRO_MIN_BUY_LAMPORTS <= was + sol:
                self.pro_traders += 1
            if user != self.creator and row["slot"] <= self.create_slot + INSIDER_SLOTS:
                self.insiders.add(user)
            if self.n_sells == 0:
                self.launch_run_mult = max(self.launch_run_mult, self.mcap_multiple)
        else:
            self.balances[user] = max(self.balances.get(user, 0) - tokens, 0)
            self.n_sells += 1

    def age_ms(self, now_ms: int) -> int:
        return now_ms - self.created_ms

    @property
    def mcap_multiple(self) -> float:
        return launch_multiple(self.curve)

    @property
    def dev_share(self) -> float:
        return self.balances.get(self.creator, 0) / TOTAL_SUPPLY

    @property
    def insider_share(self) -> float:
        held = sum(self.balances.get(w, 0) for w in self.insiders)
        return self.dev_share + held / TOTAL_SUPPLY

    @property
    def bundle_candle(self) -> bool:
        return self.launch_run_mult >= BUNDLE_CANDLE_MULT

    def top_holders(self, n: int) -> list[str]:
        """The n wallets holding the most tokens, largest first."""
        held = [w for w, b in self.balances.items() if b > 0]
        return sorted(held, key=lambda w: (-self.balances[w], w))[:n]

    def largest_holder(self) -> tuple[str | None, float]:
        """Wallet holding the most tokens (the curve itself is not a wallet here)."""
        if not self.balances:
            return None, 0.0
        wallet = max(self.balances, key=self.balances.__getitem__)
        return wallet, self.balances[wallet] / TOTAL_SUPPLY

    def snapshot(self, now_ms: int) -> dict:
        """Feature values for the decision log."""
        _, holder_share = self.largest_holder()
        return {
            "age_s": round(self.age_ms(now_ms) / 1000, 3),
            "mcap_multiple": round(self.mcap_multiple, 4),
            "progress": round(self.curve.progress, 4),
            "dev_share": round(self.dev_share, 4),
            "insider_share": round(self.insider_share, 4),
            "insiders": len(self.insiders),
            "pro_traders": self.pro_traders,
            "largest_holder_share": round(holder_share, 4),
            "launch_run_mult": round(self.launch_run_mult, 4),
            "trades": self.n_trades,
            "sells": self.n_sells,
            "tainted": self.tainted,
        }
