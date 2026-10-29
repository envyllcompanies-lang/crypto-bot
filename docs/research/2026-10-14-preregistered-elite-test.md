# Pre-registered test: the elite universes, live paper trading

Written 14 Oct 2026, before the elite universes had traded live.

## Why

Over 24 Sep-8 Oct 2026 no variant in the backtests made money. Stacking the stage-one
holder filters cut the loss from -6.8% to about -4% per trade, and the live-only signals
(top-5 balances and funding, X link, the narrative verdict) have not been tested. The
owner asked to trade only the best coins, so two strict rules replace the wide grid:

- `elite_onchain`: not a copycat, no bundled launch candle, dev at most 5%, insiders at
  most 20%, largest holder at most 4%, first 10 buyers at most 15%, creator not a serial
  launcher, top-5 SOL balances not alike, top-5 funding not clustered
  (`conviction.elite_misses`).
- `elite_thesis`: all of that, plus an X community link and a narrative verdict of 4-5
  with no red flags.

Each buys at once when the token qualifies, with two exits (+5% target and -10% stop; a
20% trailing stop and -10% stop), through Terminal and direct to the program: 8 variants
(`v1.default_variants`).

## The test

- **Data:** live paper trading from the first run with this code until 28 Oct 2026, or
  until a variant has 150 closed trades, whichever comes first. Every run is pooled
  (`cryptobot.review`).
- **Pass, per variant:** mean net return per trade above zero **and** the 95% interval's
  lower bound above zero, with at least 100 trades. Eight variants are tested at once, so
  a lower bound merely above -1% is not enough this time.
- **Also reported:** whether `elite_thesis` beats `elite_onchain` (does the thesis add
  anything?), and both against the shadow labels base rate over the same days.
- **If one passes:** a further two weeks of live paper trading with network costs
  measured on real transactions, before any talk of real money (the owner's call).
- **If none passes:** the elite rule goes no further; the result is recorded here.

## Amendments, 26 Oct 2026 (before any live result was seen)

No live result of this test had been seen when these were written: the server had not
pushed any report.

1. **Holder lookups.** Until 26 Oct (commit c86103c) the top-5 balance and funding
   checks read finalized data, about 13 seconds behind, so a bundle wallet funded in
   those seconds looked old and unrelated and could pass. From 26 Oct they read
   confirmed data, and a holder with no transaction yet counts as fresh. The judge shows
   trades before and after the fix apart; the pass rule uses all of them, as registered.
2. **The judge.** `uv run python -m cryptobot.prereg` applies these rules: live runs
   only (replays would count each trade twice), the test starts at the commit that put
   the elite variants live (14 Oct 01:05 UTC) and stops when any of the 8 variants has
   150 closed trades (by close time) or at the end of 28 Oct UTC, whichever comes first.
   Beside the registered lower bound it shows the bound that would hold for 8 tests at
   once (z = 2.73, Bonferroni): with eight tries a lucky pass is several times more
   likely than with one, so a pass that clears only the registered bound is weak.
