# Pre-registered test: V4's strategies after graduation, on the history

Written 28 Oct 2026, before any result of these rules was computed. Code:
`src/cryptobot/migrated.py`.

## Why

Source video V4 (`docs/research/source-videos.md`) makes its money after coins graduate:
it buys the reversal off the post-graduation low and sells around 5x ("bottom bidding",
also CLAUDE.md's S5), or holds for days and takes profit on the way up ("conviction
holds"). Nothing here has tested trading after graduation beyond 70 minutes.

## Data

- Every coin that graduated from a classic Pump.fun curve (SOL-quoted, not Mayhem) from
  1 Sep to 8 Oct 2026, with its whole first 72 hours of PumpSwap trading in the data:
  one-minute bars of the pool's reserves from Slinky21's `swap` table. Coins that
  graduated less than 72 hours before the data ends are left out.
- **Discovery:** graduated 1-23 Sep. **Holdout:** graduated 24 Sep onwards.

## Fills

- 0.25 SOL per trade, PumpSwap fee 1.25% a side (its starting rate; it steps down for
  larger coins, so this is a little harsh), no network costs.
- A decision is made at a bar's close and fills at the close of the next bar with
  trades, so at least a minute later, with our own price impact on the pool. A target
  or stop seen inside a bar (its high or low) is sold at the next bar's close too.
- A position still open at 72 hours is sold at the last bar.

## Rules

**Entry ("bottom bid", V4's strategy 2 and S5).** After graduation, track the price's
running high and the lowest price since that high. Buy on the first bar whose close is
at least R above that low, when the low is at least D below the high, and the coin is
inside the age window:
- D (fall from the high to the low): 60%, 70%, 80%;
- R (rise off the low): +30%, +100%;
- age at the signal: 1-3 hours (S5), or 30 minutes to 24 hours.

**Exits, bottom bid:** sell at 2x or at 5x the entry price (V4's SWolf exit), or at the
first bar below the low it bought off ("stops making new lows" broken), or at 72 hours.

**Exits, conviction hold (V4's strategy 3), same entries:** sell a third at 2x, a third
at 4x, and the rest when the price falls 60% from its highest close since entry, or at 72
hours. A second version also buys a second 0.25 SOL if the price falls 50% below the
first entry (V4 added to PUMPKIN on the way down), and both lots follow the same exits.

That is 12 entries x 4 exit sets = 48 cells.

## The test

- **Pre-set rule:** the cell with the best discovery mean net return per trade, among
  cells with at least 200 discovery trades, is judged on the holdout. It passes if its
  holdout mean is above zero and its 95% lower bound is above zero (one trade per coin
  per cell, so trades are independent coins).
- **Also reported:** every cell in both periods, the share of trades that hit each exit,
  and the base rate: buy every coin at one hour after graduation and hold to 72 hours.
- **Known gaps:** no holder data after graduation, so V4's "largest holder under 4%"
  can't be applied; no narrative (V4 picks coins by story); the constant-product k is
  the deposit's, so prices drift a few percent low by day three as fees build up.
- **If it passes:** the next step is a live test, which needs the recorder to capture
  PumpSwap trades (it doesn't yet). **If it fails:** recorded here; bottom bidding and
  conviction holds join the families that lost.

## Amendment, 28 Oct 2026, before any result was looked at

One run on the part of the data downloaded by then checked only that the code finished;
none of its numbers were read.

- **Holes in the swap files.** Some 12-hour files are missing and some hold only part of
  their swaps: 4 Sep 22:00 to 5 Sep 16:00 holds none, and 6 Sep 14:00 to 7 Sep 16:00 holds
  a small fraction of the usual count. A coin whose 72 hours cross a hole would look
  untraded there and fill late. To keep the rule above ("its whole first 72 hours of
  PumpSwap trading in the data"), an hour counts as in the data only if it holds at least
  a quarter of the median hour's swaps across the graduate pools in their windows
  (`migrated.gaps`). Coins whose 72 hours touch any other hour are left out. The report
  lists the holes and how many coins they remove.
- **Trailing stop.** "Its highest close since entry" includes the entry bar's close.

## Result, 29 Oct 2026

**Fail.** `reports/2026-10-29-after-graduation.md`. The best discovery cell
(`drop60|rise30|1-3h`, 2x target) lost 12.6% per trade on the holdout [-16.0% to
-9.1%], n 1,563. None of the 48 cells had a positive mean in either period, nor did any
of them when rerun on closes only. Bottom bidding and conviction holds after graduation
join the families that lost. No live test follows from this.
