# Data

Raw files live in `data/raw/<owner>__<dataset>/` and are never committed. Derived
files (such as the cached chain check) live in `data/derived/`.
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
- **Fetched so far:** one 12-hour sample (2026-09-12 15:14 to 2026-09-13 03:14 UTC):
  ```
  uv run python -m cryptobot.fetch Slinky21/Pumpfun_v2_dataset \
    p01_2026-08_to_2026-11/2026-09_d13-24/trade/trade_20260913T031440.parquet \
    p01_2026-08_to_2026-11/2026-09_d13-24/create/create_20260913T031440.parquet
  ```
- **Quality:** see `reports/2026-10-10-m1-data-quality.md`. Every classic trade
  matches the curve formula to within 1 raw unit, and 30 of 30 sampled trades match
  the chain exactly.

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

## Paper trading runs (ours, M6)

- **Source:** `uv run python -m cryptobot.paper live [--minutes N]` (live stream) or
  `uv run python -m cryptobot.paper replay --events DIR` (recorded events).
- **What:** one folder per run, `data/paper/<UTC start>[-replay]/`:
  - `decisions/`: every signal, fill, skip and stream gap, with the variant, mint, price
    and the variant's paper wallet balance.
  - `trades/`: every closed paper trade, with entry and exit times and prices, cost,
    proceeds, PnL, exit reason and flags.
  - `gates/`: each time a token starts or stops passing a universe's rules, with every
    rule's result (null = not evaluated) and the token's features.
  - `variants.json`: the settings of each variant. `summary.json`: counts and final
    wallet balances.
  - `events/` (live runs only): the raw stream, in the recorder's layout, plus `lookups`
    (each holder and X-link lookup: top-5 wallets, their SOL balances, first-transaction
    times and funders, and the token's X, Telegram and website links), so the run can be
    replayed exactly.
- **Report:** `uv run python -m cryptobot.m6_report [RUN_DIR]`.
