# crypto-bot

Backtester, then paper trader, then (only on explicit instruction) a live bot for Solana memecoins launched on Pump.fun. The owner is a solo builder starting with at most 1 SOL. The strategy under test is a dip-buying method taken from a YouTuber's videos. Its core claim is unproven, so this repo exists to test it before any money moves.

Full research report (verdicts, sources, math): https://claude.ai/code/artifact/9513c255-287c-4e21-b4dc-02147815670a

## Ground rules

- Order of work is fixed: backtest -> paper trade -> live. Do not write or run code that sends a real transaction until the owner says so in chat.
- Never handle secrets. No private keys, seed phrases or API keys in chat, in code, in commits or in logs. Keys live in a git-ignored `.env` on a machine the owner controls, and the owner types them in. If a task seems to need a key pasted into the session, stop and say so.
- You do not place trades. The owner runs the bot. Claude writes, tests and explains it.
- No site automation. Do not script the Terminal (trade.padre.gg) or pump.fun websites, and do not work around bot detection. Use on-chain data, the official SDK and documented APIs.
- $0 by default. Prefer free data and free tiers. Before adding anything that costs money, state the price and ask.
- No trade advice. Report what the data shows. Do not tell the owner which coins to buy. Say plainly when a result is weak or the sample is small.
- Evidence before claims. Every number in a report comes from code in this repo that someone can rerun. No hand arithmetic in conclusions.
- Dependencies: pin versions, use the official `@pump-fun/pump-sdk` or the IDLs from `pump-fun/pump-public-docs` for anything on-chain, and never copy code from unknown "Solana bot" repos. Fake ones have stolen keys through hidden dependencies.

## Stack and conventions

- Python 3.12, uv for environments, pytest for tests, ruff for lint.
- Data as Parquet, queried with Polars or DuckDB. Raw data is never committed; `data/` is git-ignored and each dataset gets a short entry in `data/README.md` (source, date range, licence, how it was fetched).
- Layout: `src/cryptobot/` (library), `tests/`, `notebooks/` (exploration only), `reports/` (dated markdown results), `docs/` (spec and research).
- Keep amounts in lamports and raw token units inside the code. Convert to SOL or USD only at the edges.
- Small commits on a branch, with tests. Commit or push only when asked.

## Facts to build on

### Bonding curve

From `pump-fun/pump-public-docs` (program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`):

- Constant product with virtual reserves: 30 SOL and 1,073,000,000 tokens at launch. 793,100,000 tokens are sold through the curve. Supply 1,000,000,000.
- Price = vSol / vTokens. Market cap in SOL = vSol^2 / 32.19.
- Launch market cap 27.96 SOL. Graduation at vSol 115 (85 real SOL), market cap 410.9 SOL, which is 14.7x launch. After graduation the token trades on PumpSwap; its pools carry a `virtual_quote_reserves` field (Sept 2026 change).
- Mayhem-mode tokens (program `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e`) have 2B supply and an agent trading at random for 24 hours. Exclude them.

### Costs

As of 8 Oct 2026 (recheck https://pump.fun/docs/fees before relying on them):

- Pump.fun: 1.25% per curve trade (0.95% protocol + 0.30% creator). PumpSwap starts at 1.25% and steps down with market cap.
- Terminal: 1% per trade, less 10% cashback (35% with a referral code).
- Network: priority fee plus tip per transaction, fixed in SOL, so it hurts small trades most. About 0.002 SOL of refundable rent per new token account.
- Round trip on the curve: about 2.5% direct to the program, 3.8-4.3% through Terminal, before network costs.

### Thresholds

Thresholds are multiples of launch market cap, never dollars. The source videos quote dollars that only held at the SOL price on the day each was recorded, and that price differs by video: V1 puts launch at $4-5K, which implies SOL at about $143-179, not $86. Convert each video's levels with its own SOL price. Claims from each video are in `docs/research/source-videos.md`.

| Level in the videos (SOL at $86; source video to confirm) | Multiple of launch |
|---|---|
| New-pairs minimum, $3.5K | 1.5x |
| Final-stretch minimum, $6K | 2.5x |
| Final-stretch "floor", $7-8K | 2.9-3.3x |
| "10K", where new-pair bundles are said to sell | 4.2x |
| "20K", where final-stretch bundles are said to sell | 8.3x |
| Graduation | 14.7x |

## The hypotheses to test

Five setups, all the same bet: early holders dump, we buy the drop, we sell the recovery. Nobody has published how often the recovery comes.

| ID | Setup | Entry trigger to formalise |
|---|---|---|
| S1 | New-pairs floor | Token under ~6 min old runs to ~2.5-3.8x launch, then falls 50-60% toward launch |
| S2 | Final-stretch floor | Runs to ~7.5-8.3x, falls ~60% to ~3x, stops making new lows |
| S3 | 40% dip (V1's core method) | New high, then a 40-50% drop from it, in New Pairs and Final Stretch alike. Test with and without a ~6x minimum peak, since V1 sets none. V1's control case is shallow dips: he calls his example 10%, but it is really 25% |
| S4 | Tracked developer | Deployer with past graduates; price goes sideways near ~4x after snipers. Not from V1/V2: V2 hides dev history as useless |
| S5 | Migrated range | 1-3 hours old, post-graduation, down 60-70% from the high and ranging. Not from V1/V2: V1 doesn't trade migrated tokens |

**Primary setup: the V1 baseline, exactly as stated.** Test this combination first, then change one rule at a time. The full rule list is in `docs/research/source-videos.md`.

- **Universe.** Pump.fun only, no Mayhem tokens. Either:
  - New Pairs at ≥$7K market cap, or
  - Final Stretch tokens at most 40 minutes old, with dev ≤5%, "insiders" ≤20%, "pro traders" ≥10 and ≥$8K, skipping the two highest market caps in the column.
- **Final Stretch defined.** It is Terminal's "Soon" column of tokens near graduation; V1's examples sit at 87-100% of the curve. Its entry threshold isn't stated, so define it by curve progress and test a few cut-offs.
- **The age cap.** It selects tokens that ran far up the curve fast. Compare results against base rates for that group, not for all launches.
- **Checks:**
  - The X link is a community.
  - Top-5 holder SOL balances aren't alike.
  - Top-5 funding sources and times aren't clustered.
  - No bundle launch candle (a run from launch with no sells).
  - Few tracked wallets.
- **Entry.** V1's 40-50% drop from the all-time high is one candidate, not a requirement; the creator doesn't always wait for it. Test entries side by side: no dip, 25%, 40% and 50% drops, and any other entry the owner describes. Let expectancy net of costs pick, not win rate alone.
- **Exit.** Sell everything at the first price below entry. Take profit at +45%, +50% or 2x.
- **Size.** Keep V1's sizes: a 1 SOL portfolio gives 0.25 SOL on New Pairs and 0.4 SOL on Final Stretch. No trade ever exceeds **1 SOL**, used only once the wallet reaches a threshold the owner sets. The cap may rise to 1.5 SOL later, never higher. The M2 fill model must handle price impact for stakes up to 1.5 SOL.

The headline comparison is the bounce rate of bundle-filtered tokens against all tokens, at matched age and peak. If the filter doesn't raise the win rate net of costs, it isn't doing its job. Also report the age effect: compare tokens 0-10, 10-20 and 20-40 minutes old at entry, and check whether bounce rates change at the 40-minute cut.

Exits to compare for each: +45% target (V1's claimed bounce), +50% target, +100% target, -20% stop, exit at entry with a tolerance (zero tolerance is V1's own rule), stall timer (5-20 minutes), and a largest-holder-sells trigger. "Stops making new lows" and "ranging" need exact definitions; propose them and show sensitivity.

The question that matters: for each setup, how often does price reach the target before the stop, net of all costs, with realistic fills?

Realistic fills means: a decision-to-landing delay (test 1, 2 and 5 seconds), fill at the curve price after the intervening trades, our own price impact from the curve formula, and stops that fill at the first available price rather than at the stop level. A holder with about 4.4% of supply moves price -20% alone at 8.3x launch, so fixed stops will often fill far below their level.

Break-even reference, to be recomputed in code as the first test: with a +50% target and a -20% stop the strategy needs roughly a 35% win rate through Terminal, and roughly 50% if losses average -40%.

## Filters and features

Stage-one filters taken from the videos and supported by independent data:

- Reject exact duplicates of an earlier launch (name, ticker or image). Originals graduate about ten times as often as copycats.
- No entry until creation-block buyers and the developer have sold. V1 is looser and allows the dev up to 5%, so test both.
- Largest non-pool holder at most 4%; flag identical balances, shared funders (trace up to three hops) and fresh wallets among the top ten.
- Pump.fun launches only, no Mayhem tokens, age caps per setup.

Features to add and score, from the research:

- Fees paid relative to market cap.
- Count and supply share of buyers in the creation block.
- Number of trades taken to reach a curve level.
- Telegram, X and website links in metadata, with the X link classed as community, tweet or profile (V1 trades communities only).
- Bundle signs from V1: similar SOL balances, shared funding sources and clustered funding times among the top 5 holders, and a launch run with no sells.
- Count of "tracked" wallets among holders (V1 avoids tokens with many). Proxy: a list of known active or profitable wallets built from the trade data.
- Break-even at V1's trade sizes (0.05-1.5 SOL), where fixed network costs dominate.
- Share of same-transaction buy-and-sell (wash) trades.
- Membership in a known sniper ring (Kamat, arXiv:2607.02795).

Not reproducible off-platform, so replace with our own proxies: Terminal's "pro traders" count, viewer count and wallet labels.

### Social data (X and others)

The owner wants X and other social accounts used to track virality and each token's thesis. Budget is **$0**: no paid APIs.

- **Free metadata signals.** These come from each token's metadata and on-chain data:
  - X link type: community, tweet or profile.
  - Telegram and website links.
  - Copycat name, ticker or image.
- **X oEmbed (free, official, no login).** Pulls the text, author and date of single public posts linked by a token. It can't read communities. `publish.twitter.com` must be on the environment's allowed domains.
- **Manual review.** The bot lists the tokens that pass the on-chain filters, with their X links. The owner opens the links with their own X account and records a verdict: pass/fail on V1's three community checks, plus a 1-5 virality score. The bot stores verdicts next to outcomes so we can measure whether they help.
- **Not allowed:**
  - Logging a bot into an X account.
  - Unofficial scrapers such as twikit.
  - Scraping or automating x.com, or working around X's limits.

  X forbids all of these, and the account would be banned.
- **Not in the backtest.** Social features can't be backtested on the free datasets, so they are recorded live from M5 on. Log them without acting on them until forward data shows they predict outcomes.

## Data

| Source | Use | Cost | Status |
|---|---|---|---|
| Hugging Face `cryptodata/pumpfun-dataset-light` | Trade-level backtest: ~41M rows with wallet, amounts, reserves, fees | Free, CC-BY-4.0 | Not yet opened; date range unknown |
| Hugging Face `loopholetape/pumpfun-launches` | 781,574 launches, 31 Aug-24 Sep 2026, graduation and rug labels, first-minute features | Free, CC-BY-4.0 | Not yet opened; card lists known defects |
| Our own recorder on a Solana RPC websocket | Forward data we control | Free tier (Helius: 1M credits, 10 req/s) | To build |
| PumpPortal websocket | New-token and migration events | Free; token trades metered | Optional |
| PumpFunData archive | Hourly Parquet since Feb 2026 | About $24 a month of data | Ask before buying |

The environment needs `huggingface.co` (datasets) and `publish.twitter.com` (oEmbed) on its allowed domains. Treat both free datasets as unverified: check a sample of rows against the chain, and check that reserves reproduce prices with the curve formula, before trusting results.

## Milestones

- **M0 Scaffold.** Repo layout, tooling, and `curve.py` with tests that reproduce every number in "Facts to build on", plus the break-even table.
- **M1 Data.** Fetch and validate the two free datasets. Write `data/README.md` and a data-quality report.
- **M2 Replay engine.** Event-driven backtester over trade events with the fill model above. Test it on hand-built toy tokens first.
- **M3 Setup scoring.** Score the V1 baseline first, then detect S1-S5, run the exit grid, report win rate, average win and loss, expectancy per trade and per day, with sample sizes and confidence intervals. Split by week to check stability.
- **M4 Filter study.** Which filters and features change the S1-S5 results, tested out of sample.
- **M5 Recorder.** Capture live create and trade events to Parquet.
- **M6 Paper trader.** Run the surviving rules live with simulated fills and compare against the backtest.

Stop after each milestone and report before starting the next.

## Known gaps

- The report's two charts and its final source check were not finished.
- Whether Pump.fun's terms exclude US persons was not confirmed; its "Prohibited Jurisdiction" definition could not be read. Terminal's terms could not be retrieved.
- Sandwich-attack data covers AMM pools, not the bonding curve.
- Some supporting figures come from data vendors and one personal blog; the report marks which.
