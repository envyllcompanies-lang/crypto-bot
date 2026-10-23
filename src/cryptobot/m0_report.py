"""Print the M0 report: curve facts, video levels and break-even win rates.

Run with `uv run python -m cryptobot.m0_report > reports/<date>-m0-curve-and-breakeven.md`.
Every number in the report comes from this module.
"""

from __future__ import annotations

from cryptobot.costs import (
    TOKEN_ACCOUNT_RENT_LAMPORTS,
    VENUES,
    break_even_win_rate,
    round_trip_cost,
)
from cryptobot.curve import (
    LAUNCH_MARKET_CAP,
    Curve,
    graduation_curve,
    implied_sol_price,
    launch_multiple,
    to_lamports,
    to_sol,
    usd_to_launch_multiple,
)

# Exits to tabulate: (label, win price move, loss price move).
EXITS = (
    ("+45% / stop fills 5% below entry", 0.45, -0.05),
    ("+45% / stop fills 10% below entry", 0.45, -0.10),
    ("+45% / stop fills 20% below entry", 0.45, -0.20),
    ("+45% / stop fills 30% below entry", 0.45, -0.30),
    ("+50% / -20%", 0.50, -0.20),
    ("+50% / -40%", 0.50, -0.40),
    ("+100% / -20%", 1.00, -0.20),
)
# V1's largest buys for a 0.1, 0.5 and 1 SOL portfolio, the 1 SOL cap per trade,
# and the possible later 1.5 SOL cap.
STAKES_SOL = (0.05, 0.1, 0.25, 0.4, 1.0, 1.5)
# Token-account rent: refunded if the account is closed after the sell, lost if not.
RENT_MODES = (("rent refunded", True), ("rent lost", False))
# Priority fee plus tip per transaction. Unknown until measured on-chain (M5).
NETWORK_SOL_PER_TX = (0.0, 0.0005, 0.001, 0.002)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _progress_label(multiple: float) -> str:
    if multiple < 1:
        return "below launch"
    if multiple >= launch_multiple(graduation_curve()):
        return "graduated"
    return _pct(Curve.at_multiple(multiple).progress)


def curve_facts() -> list[str]:
    grad = graduation_curve()
    return [
        "## Curve facts",
        "",
        "| Fact | Value |",
        "|---|---|",
        f"| Launch market cap | {to_sol(LAUNCH_MARKET_CAP):.2f} SOL |",
        f"| Graduation virtual SOL | {to_sol(grad.virtual_sol):.3f} SOL |",
        f"| Graduation real SOL | {to_sol(grad.real_sol):.3f} SOL |",
        f"| Graduation market cap | {to_sol(grad.market_cap_lamports()):.2f} SOL |",
        f"| Graduation multiple of launch | {launch_multiple(grad):.2f}x |",
        "",
    ]


def video_levels() -> list[str]:
    lines = [
        "## Video dollar levels as launch multiples",
        "",
        "V1 puts launch at $4K-5K, which implies SOL at "
        f"${implied_sol_price(4_000):.0f}-${implied_sol_price(5_000):.0f}.",
        "",
        "| Level | x launch at SOL $86 | x launch, V1 ($4K launch) | x launch, V1 ($5K launch) "
        "| Curve progress, V1 ($4K / $5K) |",
        "|---|---|---|---|---|",
    ]
    for usd in (3_500, 6_000, 7_000, 8_000, 10_000, 14_000, 20_000, 40_000, 58_000):
        at86 = usd_to_launch_multiple(usd, 86)
        m4 = usd_to_launch_multiple(usd, implied_sol_price(4_000))
        m5 = usd_to_launch_multiple(usd, implied_sol_price(5_000))
        progress = [_progress_label(m) for m in (m4, m5)]
        lines.append(f"| ${usd:,} | {at86:.2f}x | {m4:.2f}x | {m5:.2f}x | {' / '.join(progress)} |")
    return [*lines, ""]


def round_trips() -> list[str]:
    lines = ["## Round-trip fees (no network costs)", "", "| Venue | Round trip |", "|---|---|"]
    lines += [f"| {v.name} | {_pct(round_trip_cost(v))} |" for v in VENUES]
    return [*lines, ""]


def break_even_tables() -> list[str]:
    lines = [
        "## Break-even win rates",
        "",
        "Win rate needed for zero expected profit, assuming every win and every loss hits the",
        "stated price move exactly. Price impact and slipped fills are not included yet (M2).",
        "Network cost per transaction is an assumption until measured. Token-account rent is",
        f"{TOKEN_ACCOUNT_RENT_LAMPORTS / 1e9:.6f} SOL per trade: refunded if the account is closed",
        "after the sell, lost if not.",
        "",
    ]
    for venue in VENUES:
        for rent_label, refunded in RENT_MODES:
            lines += [f"### {venue.name}, {rent_label}", ""]
            cols = " | ".join(f"{n} SOL/tx" for n in NETWORK_SOL_PER_TX)
            lines += [
                f"| Exit | Stake (SOL) | {cols} |",
                "|---|---|" + "---|" * len(NETWORK_SOL_PER_TX),
            ]
            for label, win, loss in EXITS:
                for stake in STAKES_SOL:
                    cells = [
                        _pct(
                            break_even_win_rate(
                                win,
                                loss,
                                venue,
                                to_lamports(stake),
                                to_lamports(n),
                                TOKEN_ACCOUNT_RENT_LAMPORTS,
                                refunded,
                            )
                        )
                        for n in NETWORK_SOL_PER_TX
                    ]
                    lines.append(f"| {label} | {stake} | " + " | ".join(cells) + " |")
            lines.append("")
    return lines


def main() -> None:
    lines = [
        "# M0: curve facts and break-even win rates",
        "",
        "Generated by `uv run python -m cryptobot.m0_report`. Do not edit by hand.",
        "",
        *curve_facts(),
        *video_levels(),
        *round_trips(),
        *break_even_tables(),
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
