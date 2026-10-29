# Pre-registered test: late-curve entries after a top leader's buy, live paper trading

Written 14 Oct 2026, before the late-curve universes had traded live.

## Why

Every strategy family lost money in history (1 Sep-8 Oct 2026, 2 s delay, fees
included): dip buying, momentum, late-curve entries, buying at launch and copy trading
(`reports/2026-10-14-strategy-screen.md`, `reports/2026-10-14-copy-trading.md`).

Two signals were the least bad:
- buying when curve progress first reaches 90%;
- the 50 most profitable wallets.

Combined, they came close to break-even out of sample (`reports/2026-10-14-confluence.md`):

| Rule, direct fees, 24 Sep-8 Oct holdout | Trades | Mean [95% CI] |
|---|---|---|
| 90% progress, +25% target / -15% stop, with a top-50 leader's earlier buy | 2,281 | -0.1% [-1.1% to +0.8%] |
| The same without the leader rule | 5,587 | -2.2% [-3.1% to -1.4%] |
| 90% progress, 25% trailing stop / -30% stop, with leader | 2,281 | -0.7% [-2.0% to +0.7%] |

The leader rule improved every exit in the holdout, by 1-4 points. No version of this
rule was positive, so this test asks whether the near break-even holds live.

## The rule (`v1.late_variants`)

- **Universes:**
  - `late_p90`: buy on the trade that first takes the curve to 90% progress, if the
    token is at most 60 minutes old.
  - `late_p90_leader`: the same, and one of the 50 most profitable wallets on the
    nightly tracked list (`tracked.load_top`) must have bought the token before.
- **Exits:** `tp25|sl15` (+25% target, -15% stop) and `trail25|sl30` (25% trailing stop,
  -30% stop). Both have a 60-minute time limit and the engine's 5-minute quiet exit.
- **Trading:** 0.25 SOL, direct to the program, 2 s delay. Shadow variants with no
  wallet limit, so every qualifying token is traded.

**Differences from the backtest:** the live leader list is the nightly tracked list.
That list is built from the server's own recordings (1 day by default,
`CRYPTOBOT_TRACKED_DAYS`), whereas the backtest picked leaders from 23 days of history.
A shorter window may pick worse leaders.

## The test

- **Data:** live paper trading from the first run with this code, pooled across runs
  (`cryptobot.review`). The test runs until `late_p90_leader|tp25|sl15|d2s|direct` has
  300 closed trades or until 28 Oct 2026, whichever comes first. The backtest made
  about 150 such trades a day.
- **Pass:** `late_p90_leader|tp25|sl15|d2s|direct` needs:
  - at least 300 trades;
  - a mean net return per trade above zero;
  - a 95% lower bound above zero.

  The trailing-stop variant is secondary.
- **Also reported:**
  - leader minus control (`late_p90`), with an interval. Does the leader rule still
    help live?
  - the share of positions still open at graduation, which are valued at the final
    curve price. After graduation most pools collapse within minutes (screen.py
    `--pools`), so that valuation flatters these positions.
- **If it passes:** two more weeks of paper trading with measured network costs and a
  check on the graduation valuation. Real money is the owner's call after that, never
  before.
- **If it fails:** the late-curve family goes no further. Without a faster or
  differently priced feed, no strategy family tested here makes money after fees.

## Amendments, 26 Oct 2026 (before any live result was seen)

No live result of this test had been seen when these were written: the server had not
pushed any report.

1. **The leader list was ten minutes, not a day.** "1 day by default" above was not
   what ran: until 26 Oct (commit c86103c) the nightly list was built at 00:10 UTC from
   the newest day folder only, about ten minutes of trades. From 26 Oct it covers the
   last 24 hours. The judge shows trades before and after the fix apart; the pass rule
   uses all of them, as registered.
2. **How a pass is read.** The backtest family this rule comes from made its money on
   trades still open at graduation (`reports/2026-10-26-quick-hold-graduation.md`), and
   live paper trading values those at the final curve price, decided and filled at
   once. The pass rule stays as written, but a pass whose mean without graduation exits
   is zero or below is reported as "pass only through graduation exits", not as
   evidence of an edge.
3. **The judge.** `uv run python -m cryptobot.prereg` applies these rules: live runs
   only (no replays), the test starts at the commit that put the variants live (14 Oct
   02:30 UTC), it stops at the 300th closed trade of
   `late_p90_leader|tp25|sl15|d2s|direct` (by close time) or at the end of 28 Oct UTC,
   whichever comes first. "Leader minus control" uses a Welch interval; the leader
   variant's tokens are a subset of the control's, so that interval is conservative.
