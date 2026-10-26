"""Judge the pre-registered live paper-trading tests exactly as written.

Each document in docs/research/2026-10-14-preregistered-*.md fixed, before the variants
traded: the variant judged, when the test starts, when it stops (a number of closed
trades or a date, whichever comes first) and the pass rule (at least N trades, a mean
net return per trade above zero, and the 95% interval's lower bound above zero). This
module applies those rules to the live runs only (review.load: no replays, no
backtests, a trade recorded twice counted once) and adds the numbers each document
promised: leader minus control, the base rate, the share of trades open at graduation
and the mean without them, plus the amendments dated in the documents.

While a test is running the numbers are provisional, not a verdict. Nothing here may be
tuned after looking at results: a change goes into the document as a dated amendment
first.

Usage: uv run python -m cryptobot.prereg [--root data/paper]
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from cryptobot.m6_report import Z95, _pct, md_cell
from cryptobot.paper import PAPER
from cryptobot.review import load


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


# 26 Oct 2026 (commit c86103c): the nightly leader list covered ~10 minutes instead of
# a day, and holder lookups read finalized data. Trades before and after are shown apart.
FIX_MS = _ms("2026-10-26T01:36:55+00:00")
ELITE = tuple(
    f"elite_{kind}|dip0|{exit_}|sl10|d2s{venue}"
    for kind in ("onchain", "thesis")
    for exit_ in ("tp5", "trail20")
    for venue in ("", "|direct")
)  # fmt: skip


@dataclass(frozen=True)
class Test:
    name: str
    doc: str
    primary: tuple[str, ...]  # judged by the pass rule
    start_ms: int  # the commit that put these variants live
    end_ms: int  # the date in the document (through the end of that UTC day)
    stop_trades: int  # ...or when a primary variant has this many closed trades
    min_trades: int
    secondary: tuple[str, ...] = ()  # reported, not judged
    control: dict[str, str] = field(default_factory=dict)  # primary -> its control
    base_rate: str | None = None  # the shadow labels variant over the same days
    pairs: tuple[tuple[str, str], ...] = ()  # (a, b): report a minus b


TESTS = (
    Test(
        "elite",
        "docs/research/2026-10-14-preregistered-elite-test.md",
        primary=ELITE,
        start_ms=_ms("2026-10-14T01:05:24+00:00"),
        end_ms=_ms("2026-10-29T00:00:00+00:00"),
        stop_trades=150,
        min_trades=100,
        base_rate="labels|tp5|sl10|d2s",
        pairs=tuple((t, t.replace("elite_thesis", "elite_onchain")) for t in ELITE[4:]),
    ),
    Test(
        "late_leader",
        "docs/research/2026-10-14-preregistered-late-leader-test.md",
        primary=("late_p90_leader|tp25|sl15|d2s|direct",),
        start_ms=_ms("2026-10-14T02:30:11+00:00"),
        end_ms=_ms("2026-10-29T00:00:00+00:00"),
        stop_trades=300,
        min_trades=300,
        secondary=("late_p90_leader|trail25|sl30|d2s|direct",),
        control={
            "late_p90_leader|tp25|sl15|d2s|direct": "late_p90|tp25|sl15|d2s|direct",
            "late_p90_leader|trail25|sl30|d2s|direct": "late_p90|trail25|sl30|d2s|direct",
        },
    ),
    Test(
        "quick_hold",
        "docs/research/2026-10-14-preregistered-quick-hold-test.md",
        primary=("quick_p90_leader|hold1m|d2s|direct",),
        start_ms=_ms("2026-10-14T04:24:23+00:00"),
        end_ms=_ms("2026-11-05T00:00:00+00:00"),
        stop_trades=300,
        min_trades=300,
        control={"quick_p90_leader|hold1m|d2s|direct": "quick_p90|hold1m|d2s|direct"},
    ),
)
# Eight elite variants at once: also show the lower bound that would hold for 8 tests
# (Bonferroni). Not part of the registered pass rule; reported so a lucky pass shows.
Z_ELITE_8 = 2.734


def graduated(df: pl.DataFrame) -> pl.Expr:
    """Trades closed because the curve completed (valued at its final price live)."""
    exit_reason = pl.col("exit_reason").fill_null("") == "graduated"
    if "flags" in df.columns:
        return exit_reason | pl.col("flags").fill_null("").str.contains("graduated")
    return exit_reason


def stop_time(trades: pl.DataFrame, test: Test) -> tuple[int, str]:
    """When the test stops: the date, or the close of the first primary variant's
    `stop_trades`-th trade, whichever comes first."""
    cut, why = test.end_ms, "date"
    for v in test.primary:
        mine = (pl.col("variant") == v) & (pl.col("entry_decided_ts_ms") >= test.start_ms)
        closed = trades.filter(mine).sort("exit_ts_ms")["exit_ts_ms"]
        if closed.len() >= test.stop_trades:
            at = int(closed[test.stop_trades - 1])
            if at < cut:
                cut, why = at, f"{test.stop_trades} trades ({v})"
    return cut, why


def window(trades: pl.DataFrame, test: Test, cut: int) -> pl.DataFrame:
    return trades.filter(
        (pl.col("entry_decided_ts_ms") >= test.start_ms) & (pl.col("exit_ts_ms") <= cut)
    )


def stats(r: pl.Series, z: float = Z95) -> tuple[int, float | None, float | None]:
    """Trades, mean, and the interval's lower bound (None under two trades)."""
    n = r.len()
    if n == 0:
        return 0, None, None
    mean = float(r.mean())
    if n < 2:
        return n, mean, None
    return n, mean, mean - z * float(r.std()) / n**0.5


def diff(a: pl.Series, b: pl.Series) -> str:
    """a's mean minus b's, with a 95% interval (Welch). Where b contains a's tokens
    (a leader universe is a subset of its control) the interval is conservative."""
    if a.len() < 2 or b.len() < 2:
        return "n/a (fewer than 2 trades)"
    d = float(a.mean()) - float(b.mean())
    se = (float(a.var()) / a.len() + float(b.var()) / b.len()) ** 0.5
    return f"{_pct(d)} [{_pct(d - Z95 * se)} to {_pct(d + Z95 * se)}]"


def _fmt(n: int, mean: float | None, lo: float | None) -> str:
    if mean is None:
        return f"{n:,} | n/a | n/a"
    return f"{n:,} | {_pct(mean)} | {'n/a' if lo is None else _pct(lo)}"


def judge(trades: pl.DataFrame | None, test: Test, now_ms: int) -> list[str]:
    lines = [f"## {test.name}", "", f"Rules: `{test.doc}`.", ""]
    if trades is None or trades.is_empty():
        return [*lines, "No live trades yet.", ""]
    cut, why = stop_time(trades, test)
    finished = cut <= now_ms
    w = window(trades, test, cut)
    stamp = datetime.fromtimestamp(cut / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(
        f"**{'Finished' if finished else 'Running (provisional, not a verdict)'}.** "
        f"Stops at {stamp} ({why}). Pass: at least {test.min_trades} trades, mean above "
        "zero and the 95% lower bound above zero."
    )
    lines += [
        "",
        "| Variant | Trades | Mean | 95% lower bound | Verdict |",
        "|---|---|---|---|---|",
    ]
    for v in (*test.primary, *test.secondary):
        n, mean, lo = stats(w.filter(pl.col("variant") == v)["net_return"])
        if v in test.secondary:
            verdict = "secondary, not judged"
        elif n < test.min_trades:
            verdict = f"too few trades ({n} of {test.min_trades})" + ("" if finished else " so far")
        else:
            ok = mean is not None and lo is not None and mean > 0 and lo > 0
            verdict = ("**pass**" if ok else "**fail**") + ("" if finished else " (provisional)")
            t = w.filter(pl.col("variant") == v)
            _, rest, _ = stats(t.filter(~graduated(t))["net_return"])
            if ok and (rest is None or rest <= 0):  # amendment of 26 Oct (see the document)
                verdict += ", but only through graduation exits: not evidence of an edge"
        lines.append(f"| {md_cell(v)} | {_fmt(n, mean, lo)} | {verdict} |")
    if test.name == "elite":
        lines += ["", f"Lower bound for 8 tests at once (z = {Z_ELITE_8}, not the pass rule):", ""]
        lines += ["| Variant | Trades | Mean | Lower bound, 8 tests |", "|---|---|---|---|"]
        for v in test.primary:
            n, mean, lo = stats(w.filter(pl.col("variant") == v)["net_return"], Z_ELITE_8)
            lines.append(f"| {md_cell(v)} | {_fmt(n, mean, lo)} |")
    if test.control:
        lines += ["", "Leader minus control, same window (registered comparison):", ""]
        for v, c in test.control.items():
            a = w.filter(pl.col("variant") == v)["net_return"]
            b = w.filter(pl.col("variant") == c)["net_return"]
            lines.append(f"- {md_cell(v)} minus {md_cell(c)}: {diff(a, b)}")
    if test.pairs:
        lines += ["", "Does the thesis add anything? `elite_thesis` minus `elite_onchain`:", ""]
        for a, b in test.pairs:
            ra = w.filter(pl.col("variant") == a)["net_return"]
            rb = w.filter(pl.col("variant") == b)["net_return"]
            lines.append(f"- {md_cell(a)}: {diff(ra, rb)}")
    if test.base_rate:
        n, mean, lo = stats(w.filter(pl.col("variant") == test.base_rate)["net_return"])
        lines += ["", f"Base rate over the same days, {md_cell(test.base_rate)}: "
                  f"{n:,} trades, mean {_pct(mean) if mean is not None else 'n/a'}."]  # fmt: skip
    # Amendments (see the documents): graduation exits, and the 26 Oct fixes.
    lines += [
        "",
        "Graduation and the 26 Oct fixes (amendments of 26 Oct, before any live result was "
        "seen):",
        "",
        "| Variant | Open at graduation | Mean without them | Before 26 Oct fix | After |",
        "|---|---|---|---|---|",
    ]
    for v in (*test.primary, *test.secondary):
        t = w.filter(pl.col("variant") == v)
        if t.is_empty():
            continue
        g = t.filter(graduated(t))
        _, rest_mean, _ = stats(t.filter(~graduated(t))["net_return"])
        before = stats(t.filter(pl.col("entry_decided_ts_ms") < FIX_MS)["net_return"])
        after = stats(t.filter(pl.col("entry_decided_ts_ms") >= FIX_MS)["net_return"])
        lines.append(
            f"| {md_cell(v)} | {_pct(g.height / t.height)} | "
            f"{_pct(rest_mean) if rest_mean is not None else 'n/a'} | "
            f"{before[0]:,} at {_pct(before[1]) if before[1] is not None else 'n/a'} | "
            f"{after[0]:,} at {_pct(after[1]) if after[1] is not None else 'n/a'} |"
        )
    return [*lines, ""]


def report(trades: pl.DataFrame | None, now_ms: int) -> str:
    lines = [
        "# Pre-registered live tests",
        "",
        "Generated by `uv run python -m cryptobot.prereg`. Live paper runs only (no replays "
        "or backtests); each test applies its document's rules unchanged.",
        "",
    ]
    for test in TESTS:
        lines += judge(trades, test, now_ms)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=PAPER)
    args = parser.parse_args(argv)
    trades, _ = load(args.root)
    print(report(trades, int(time.time() * 1000)))


if __name__ == "__main__":
    main()
