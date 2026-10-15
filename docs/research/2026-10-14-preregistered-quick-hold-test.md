# Pre-registered test: the quick hold at 90% after a top leader, live paper trading

Written 14 Oct 2026, before these variants had traded live.

## Why

**History tested.** With PumpSwap fills after graduation (`pumpswap.py`,
`screen.py --timing --pool-fills`), a search over 576 rules found one family that was
positive in both halves of 1 Sep-8 Oct 2026. The rules in it:
- buy late on the curve (85-95% progress);
- only after one of the 50 most profitable tracked wallets has bought;
- sell within minutes.

All other rules lost money (`reports/2026-10-14-optimize-pool-d2.md`, 2 s delay,
direct fees).

**The steadiest cell.** Its profit is not carried by a few trades, and few of its trades
reach graduation:

| Buy at first 90% crossing, token 5-15 min old, top-50 leader bought first, hold 1 min | Trades | Mean | Median | Graduated |
|---|---|---|---|---|
| 1-23 Sep | 821 | +1.8% | -1.1% | 7% |
| 24 Sep-8 Oct | 624 | +2.5% [+0.1% to +4.8%] | +0.7% | 9% |

**Why this test is needed.** This cell was found after looking at both halves. It ranked
9th of 576 on the first half, so it may be luck. The search's own pre-set test failed:
its first-half winner lost 6.2% afterwards. Only data it has never seen can settle it.

## The rule (`v1.quick_variants`)

- **Universes:**
  - `quick_p90_leader`: buy on the trade that first takes the curve to 90% progress, if
    the token is 5-15 minutes old then, and one of the 50 most profitable wallets on the
    nightly tracked list bought it earlier.
  - `quick_p90`: the same without the leader rule, as the control.
- **Exit:** sell 60 seconds after the buy (stall timer). No target, no stop.
- **Trade size and fees:** 0.25 SOL, direct to the program, 2 s delay. Shadow variants:
  every qualifying token is traded, with no wallet limit.

**Known differences from the backtest:**
- The live leader list comes from 1 day of the server's recordings, not 23 days of
  history.
- Live paper trading still values a position open at graduation at the final curve
  price, because the recorder does not capture PumpSwap trades yet. That was 7-9% of
  trades in the backtest; the report will show this share.

## The test

- **Data:** live paper trading from the first run with this code, pooled across runs.
  The test runs until `quick_p90_leader|hold1m|d2s|direct` has 300 closed trades or
  until 4 Nov 2026, whichever comes first. The backtest made about 40 a day.
- **Pass:** at least 300 trades, a mean net return per trade above zero, and a 95%
  lower bound above zero.
- **Also reported:**
  - leader minus control, with an interval;
  - the share of trades open at graduation;
  - the mean with those trades left out.
- **If it passes:** add PumpSwap recording and run two more weeks with network costs
  measured. Real money is the owner's call after that.
- **If it fails:** record the result here. That would close the last lead from the
  history: no rule found on 1 Sep-8 Oct with free data and a 2 s delay would then have
  held up out of sample.
