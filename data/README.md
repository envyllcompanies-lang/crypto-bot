# Data

Raw files live in `data/raw/<owner>__<dataset>/` and are never committed. Derived
files (such as the cached chain check) live in `data/derived/`. The tracked-wallet list is
`data/derived/tracked_wallets.parquet` (with `tracked_wallets.json` for how it was built),
rebuilt nightly by `uv run python -m cryptobot.tracked` from recent recordings.
Creator records (launches and graduates per creator) are `data/derived/creators.parquet`,
rebuilt nightly by `uv run python -m cryptobot.creators` from the last 14 days.
Labelled outcomes (each eligible token's features at decision time and its result) are
`data/derived/labels.parquet`, rebuilt nightly by `uv run python -m cryptobot.labels`
from every paper run.
You never need to fetch by hand: analysis code calls `cryptobot.fetch.ensure`, which
downloads any missing file from Hugging Face on first use (free, no account) and
reuses it after that. Nothing here is uploaded to GitHub. Fetch with
`uv run python -m cryptobot.fetch <dataset> [<path-prefix> ...]`, which resumes cut
transfers and checks every file's size. Downloaded files are data only: never
import or execute them.

## Slinky21/Pumpfun_v2_dataset (trade level)

- **Source:** https://huggingface.co/datasets/Slinky21/Pumpfun_v2_dataset
- **What:** decoded Pump.fun program events, one table per event type. These are
  `trade` (every curve buy and sell, with wallet, amounts, reserves after the trade
  and fees), `create`, `graduate`, `swap` (PumpSwap), `txmeta`, `program` and `other`.
  Parquet, in 12-hour files.
- **Date range:** folders cover 2026-08-01 to 2026-10-12 (`p01_2026-08_to_2026-11`);
  still being updated.
- **Size:** about 363 GB in total. `trade` is 36.3 GB, `create` 0.6 GB, `swap` 277.5 GB.
- **Licence: none stated.** The dataset has no README and no licence field. Use it for
  private research only, never redistribute it, and confirm terms before relying on it.
- **On the server (11 Oct 2026, the owner's OK for private research):** `trade` and
  `create` for 25 Sep to 8 Oct 2026 (folders `2026-09_d25-end` and `2026-10_d01-12`,
  54 files, 10.1 GB), for the M3 backtest (`backtest.py`, reading through `slinky.py`).
  Within one transaction, trades on the same token are not always listed in the order
  they ran; `slinky.chain_order` restores it from the reserves (on 7 Oct 16:00-17:00
  this took tokens with a broken reserve chain from 100 to 0). Rows with zero SOL
  reserves all belong to curves quoted in other assets (4,673 of 42,142 tokens on 7-8
  Oct) and are dropped.
- **First sample:** one 12-hour sample (2026-09-12 15:14 to 2026-09-13 03:14 UTC):
  ```
  uv run python -m cryptobot.fetch Slinky21/Pumpfun_v2_dataset \
    p01_2026-08_to_2026-11/2026-09_d13-24/trade/trade_20260913T031440.parquet \
    p01_2026-08_to_2026-11/2026-09_d13-24/create/create_20260913T031440.parquet
  ```
- **Quality:** see `reports/2026-10-10-m1-data-quality.md`. Every classic trade
  matches the curve formula to within 1 raw unit, and 30 of 30 sampled trades match
  the chain exactly.

- **Post-graduation pools (`data/derived/pumpswap/`, 14 Oct 2026):** `uv run python -m
  cryptobot.pumpswap --from 2026-09-01` streams the `graduate` table (all of it, 66,855
  graduations) and the `swap` table (73 files for 1 Sep-8 Oct, about 2.3 GB each; each is
  deleted once sliced). It keeps swaps of at least 0.01 SOL in pools that graduated, for
  70 minutes after graduation: about 1.6M swaps per 12 hours, 1.5 GB in all. `screen.py
  --pool-fills` sells positions open at graduation into these pools.

## loopholetape/pumpfun-launches (launch level)

- **Source:** https://huggingface.co/datasets/loopholetape/pumpfun-launches
- **What:** one row per launch with outcomes (graduated, rugged, peak reserve) and
  first-minute features. No individual trades.
- **Date range:** 2026-08-31 to 2026-10-03. `launches_legacy` runs to 2026-09-26 and
  undercounts buys by about 48%; `launches` starts 2026-09-27 and is complete.
- **Licence:** CC BY 4.0 (attribution: loopholetape).
- **Fetch:** `uv run python -m cryptobot.fetch loopholetape/pumpfun-launches` (0.16 GB).
- **Known issues:** the card's KNOWN_ISSUES.md, copied into the download.

## cryptodata/pumpfun-dataset-light (planned, unavailable)

The trade dataset named in the original plan returns HTTP 401 and does not appear in
Hugging Face search, so it is either private or deleted. Slinky21 replaces it.

## Live recorder (our own, M5)

- **Source:** Pump.fun program logs streamed from the free public RPC
  (`wss://api.mainnet-beta.solana.com`, `logsSubscribe`), decoded with the field lists
  of `idl/pump.json` from `pump-fun/pump-public-docs`. A keyed RPC can be used by
  setting `CRYPTOBOT_WS_URL` in `.env`; the URL is never printed.
- **What:** `data/live/{create,trade,sessions}/<UTC date>/*.parquet`. `sessions` holds
  one row per connection (start, end, transactions, failed, truncated logs, end reason)
  so coverage gaps are recorded.
- **Date range:** whatever has been recorded on this machine.
- **Licence:** public on-chain data recorded by us.
- **Fetch:** `uv run python -m cryptobot.recorder [--minutes N]`; check it with
  `uv run python -m cryptobot.m5_report`.

## Social signals (ours, live, from 14 Oct 2026)

- **Source:** `uv run python -m cryptobot.social run` (crypto-bot-social.service) polls
  free public APIs: DexScreener token boosts and profiles (every 2 min) and community
  takeovers (5 min), GeckoTerminal trending Solana pools (5 min), CoinGecko trending
  searches (10 min), the Google Trends daily RSS feed for the US (15 min), the 4chan /biz/
  catalog (official read-only API, 1 min), Bluesky's Jetstream post stream (counts over
  the last hour, every 5 min) and Wikipedia's top English articles for the previous day
  (6 h, the official pageview API). Sources and why others were left out:
  `reports/2026-10-14-social-sources.md`.
- **What:** every snapshot as `data/live/social/<source>/<UTC date>/<ms>.parquet` (kind,
  key, value, text, received_ms), deleted after `CRYPTOBOT_KEEP_DAYS`; the current state
  in `data/derived/social_now.json`, which the paper trader records in each run's
  `events/social` table.
- **Licence:** each provider's API terms; not redistributed.

## Wikipedia top articles (cached)

- `data/derived/attention/wiki_top_<date>.parquet`: each UTC day's top 1,000 English
  Wikipedia articles from the Wikimedia pageview API (free; CC0 data), fetched by
  `attention.py` with a descriptive User-Agent at one request per day of data.

## Paper trading runs (ours, M6)

- **Source:** `uv run python -m cryptobot.paper live [--minutes N]` (live stream) or
  `uv run python -m cryptobot.paper replay --events DIR` (recorded events).
- **What:** one folder per run, `data/paper/<UTC start>[-replay]/`:
  - `decisions/`: every signal, fill, skip and stream gap, with the variant, mint, price
    and the variant's paper wallet balance.
  - `trades/`: every closed paper trade, with entry and exit times and prices, cost,
    proceeds, PnL, exit reason and flags.
  - `gates/`: each time a token starts or stops passing a universe's rules, with every
    rule's result (null = not evaluated) and the token's features (including its name and
    symbol cut to 40 characters, copycat flag, X link and narrative; for the conviction
    universe, the conviction score, tier and points per component, or why it is blocked).
  - `variants.json`: the settings of each variant. `summary.json`: counts and, per
    variant, cash, open positions marked to market and equity.
  - `events/` (live runs only): the raw stream, in the recorder's layout, plus `lookups`
    (each holder and X-link lookup: top-5 wallets, their SOL balances, first-transaction
    times and funders, the token's X, Telegram and website links and description cut to
    300 characters, and, when an LLM key is set, the narrative judge's verdict as
    `narrative` {score 1-5, theme, red_flags, reason, provider, model} or the reason it
    gave none as `narrative_error`), so the run can be replayed exactly.
- **Report:** `uv run python -m cryptobot.m6_report [RUN_DIR]`.
