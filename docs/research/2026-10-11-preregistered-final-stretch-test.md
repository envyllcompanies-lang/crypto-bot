# Pre-registered test: Final Stretch 87%, trailing stop, direct fees

Written 11 Oct 2026, before the test data was downloaded or looked at.

## Why

The two-week backtest (24 Sep-8 Oct 2026, `reports/2026-10-11-m3-final-stretch.md`)
tried 24 Final Stretch variants. One had a positive mean: `final_stretch_87|dip0|trail30|sl20|d2s|direct`,
+0.5% per trade over 337 trades, 95% interval -4.5% to +5.4%, +2.8% in the first half
and -1.9% in the second. As the best of 24, it is probably partly luck.

## The test

- **Data:** Slinky21 trades and creates from 1 Sep to 23 Sep 2026 (`--from 2026-09-01
  --to 2026-09-24`), which no rule in this repo has been chosen on.
- **Run:** `uv run python -m cryptobot.backtest --jobs 4 --grid stretch --from 2026-09-01
  --to 2026-09-24`, unchanged code apart from the run dates.
- **One rule decides:** `final_stretch_87|dip0|trail30|sl20|d2s|direct`. The other 23
  variants are reported but do not count.
- **Pass:** mean net return per trade above zero **and** the 95% interval's lower bound
  above -1%, with at least 200 trades. Anything else is a fail.
- **If it passes:** it becomes a paper-trading wallet and needs two more weeks of live
  results, net of measured network costs, before any talk of real money.
- **If it fails:** no Final Stretch rule from this grid goes forward.

## Result (12 Oct 2026): fail

`reports/2026-10-12-preregistered-final-stretch-result.md`, 1-23 Sep 2026. The rule
`final_stretch_87|dip0|trail30|sl20|d2s|direct` closed 235 trades with a mean net return
of -15.9% per trade (95% interval -20.7% to -11.1%), against +0.5% on the data it was
picked from. None of the 24 Final Stretch variants had a positive mean. Per the plan, no
Final Stretch rule from this grid goes forward.

One deviation from the plan: the run used `--jobs 2` (one chunk boundary) instead of
`--jobs 4`, so two tests could share the machine. It changes only where the period is
split, not the rule, the data or the bar.

The same grid on New Pairs over the same days (exploratory, not pre-registered;
`reports/2026-10-12-new-pairs-exits-sep.md`): none of 24 variants positive; the best was
-4.4% per trade over 27,227 trades, direct fees, 25% dip, +5% target, -10% stop.
