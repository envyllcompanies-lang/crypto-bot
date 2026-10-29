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

## Amendments, 26 Oct 2026 (before any live result was seen)

No live result of this test had been seen when these were written: the server had not
pushed any report.

1. **Correction: the backtest profit was carried by graduation trades.** "Its profit is
   not carried by a few trades" above is wrong. Rerunning the cell
   (`reports/2026-10-26-quick-hold-graduation.md`) shows all of it comes from the 7-9%
   of trades still open at graduation, worth +56-59% each when sold into the PumpSwap
   pool; the other trades lost 2.3% (1-23 Sep) and 3.0% (24 Sep-8 Oct) per trade. The
   graduation check that should have shown this compared flags with "graduated" only
   and missed the pool-valued exits. Two more doubts: some pool sales used the pool's
   opening price where the swap data had gaps, and the 1-23 Sep leader list was chosen
   from wallets that profited on those same tokens, so that half was not out of sample
   for the leader rule.
2. **How a pass is read.** Live, a position open at graduation is valued at the final
   curve price, decided and filled at once: kinder than any real sale. The pass rule
   stays as written, but a pass whose mean without graduation exits is zero or below is
   reported as "pass only through graduation exits", not as evidence of an edge.
3. **The leader list was ten minutes, not a day.** Until 26 Oct (commit c86103c) the
   nightly list was built at 00:10 UTC from the newest day folder only, about ten
   minutes of trades. From 26 Oct it covers the last 24 hours. The judge shows trades
   before and after the fix apart; the pass rule uses all of them, as registered.
4. **The judge.** `uv run python -m cryptobot.prereg` applies these rules: live runs
   only (replays reproduce live trades and would count them twice), the test starts at
   the commit that put the variants live (14 Oct 04:24 UTC), it stops at the 300th
   closed trade (by close time) or at the end of 4 Nov UTC, whichever comes first.
