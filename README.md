# crypto-bot

A paper trader and backtester for a Pump.fun dip-buying strategy ("V1") taken from YouTube videos. It never trades real money: nothing in this repo builds, signs or sends a transaction, and it reads no keys. Going live with real money happens only if the owner says so.

## Status

| Milestone | State | Result |
|---|---|---|
| M0 Scaffold | Done | `reports/2026-10-10-m0-curve-and-breakeven.md` |
| M1 Data | Done | `reports/2026-10-10-m1-data-quality.md` |
| M2 Replay engine | Done | `tests/test_replay.py` |
| M5 Live recorder | Done | `reports/2026-10-10-m5-recorder.md` |
| M6 Paper trader | Built; first live session run | `reports/2026-10-10-m6-paper.md` |
| M3 Backtest (cut down) | Next | |
| M4 Filter study | Later | |

Live testing comes first (owner's call, 10 Oct 2026): M5 and M6, then a cut-down M3, then M4.

## Run it

Needs [uv](https://docs.astral.sh/uv/). Everything uses free data and the free public Solana RPC.

```bash
uv sync
uv run pytest
uv run python -m cryptobot.paper live --minutes 60
uv run python -m cryptobot.m6_report
```

| Command | What it does |
|---|---|
| `uv run python -m cryptobot.paper live [--minutes N]` | Streams Pump.fun live, paper trades every variant, saves the raw events with the run. Ctrl-C stops it cleanly. |
| `uv run python -m cryptobot.paper replay --events DIR` | Runs the same engine over recorded events (a run's `events/` folder or `data/live`). A live session replays to identical trades. |
| `uv run python -m cryptobot.m6_report [RUN_DIR]` | Report for a paper run (default: the latest). |
| `uv run python -m cryptobot.recorder [--minutes N]` | Records events only, into `data/live`. |
| `uv run python -m cryptobot.m5_report` | Checks the recorded events against the curve formula. |
| `uv run python -m cryptobot.m1_report` | Data-quality report for the free datasets (downloads what it needs). |

Final Stretch entries start 40 minutes into a run (see below), so run the paper trader for an hour or more.

**24/7 for free:** `deploy/README.md` sets it up on Google Cloud's free-tier e2-micro server (us-east1). One command installs it as a service that restarts itself and writes a report every day.

## How the paper trader works

- **Data.** It subscribes to the Pump.fun program's logs on `api.mainnet-beta.solana.com` and decodes launch and trade events with the official IDL. A keyed RPC can be used by putting `CRYPTOBOT_WS_URL` in `.env`; the URL is never printed.
- **Tokens.** Only classic launches (quoted in SOL, not Mayhem) whose launch it saw. For each it tracks the curve, every wallet's balance, the dev's share, launch-block buyers ("insiders"), a "pro trader" proxy (wallets that bought at least 0.5 SOL) and whether the launch ran up with no sells (V1's bundle sign).
- **Rules.** V1's baseline from `docs/research/source-videos.md`:
  - New Pairs at $7K or more.
  - Final Stretch tokens up to 40 minutes old with dev ≤5%, insiders ≤20%, pro traders ≥10 and $8K or more, ranked by market cap, skipping the top two and keeping the bottom four.
  - Dollar levels are converted at the SOL price V1 implies (launch at $4.5K). Final Stretch's curve threshold isn't stated, so 50%, 75% and 87% each run as their own universe.
  - The column ranking needs every token up to 40 minutes old, so Final Stretch takes no entries in a run's first 40 minutes.
  - Once a token passes every other rule, the trader looks up V1's last checks, free, once per token, and entries wait for the answer:
    - whether its X link is a community (from the token's own metadata file; no X scraping);
    - whether the top-5 holders' SOL balances look alike (several small wallets under 1 SOL);
    - whether their funding is clustered (3 or more share a funder, or were created in the last hour).
- **Variants.** Each universe runs every combination of these side by side, each variant with its own 1 SOL paper wallet:
  - entry: no dip, or a drop of 25%, 40% or 50% from the high;
  - target: +45%, +50% or +100%;
  - stop: the first price below entry (V1's rule) or -20%;
  - all at a 2-second delay.

  V1's core rule (40% dip, +45%, exit at entry) also runs at 1 s and 5 s delays, with a 10-minute stall exit, and with a largest-holder-sells exit.
- **Fills.** The M2 fill model:
  - An order lands after the delay, behind every trade that arrived first.
  - It fills on the curve those trades left, with our own price impact.
  - Stops fill at whatever price is there when the sell lands, not at the stop level.
  - Fees are charged as through Terminal with a referral code, and token-account rent is refunded.

  A test proves the paper engine gives the same trades as the M2 replay engine.
- **Not evaluated.** V1's "few tracked wallets" check needs a curated list of known traders' wallets, which we don't have, so it's recorded as "not evaluated". A lookup that fails is also recorded as "not evaluated" for that token.
- **Wallets.** Each variant starts with its own 1 SOL paper wallet; the report shows each wallet's balance.
- **Output.** `data/paper/<run>/` holds `decisions`, `trades` and `gates` as Parquet, plus `variants.json`, `summary.json` and the raw `events/`. See `data/README.md`.

## Trade size

The starting wallet is 1 SOL:
- 0.25 SOL per New Pairs trade;
- 0.4 SOL per Final Stretch trade.

Stakes can grow with the wallet, but:
- no single trade is ever more than 1 SOL;
- 1 SOL stakes are used only once the wallet passes a threshold the owner sets;
- the cap may rise to 1.5 SOL later, never higher. The code refuses anything above 1.5 SOL.

## Layout

- `src/cryptobot/`
  - **Model:** `curve.py` (bonding curve), `costs.py` (fees, rent, break-even).
  - **Engines:** `replay.py` (M2 replay engine), `strategies.py` (dip buyer), `paper.py` (M6 engine, live and replay), `features.py` (per-token live state), `v1.py` (V1 rules and variant grid).
  - **Data:** `events.py` (event decoding), `recorder.py` (live stream to Parquet), `fetch.py` (Hugging Face downloads), `validate.py` and `chain.py` (data checks).
  - **Reports:** `m0_report.py`, `m1_report.py`, `m5_report.py`, `m6_report.py`.
- `tests/`: pytest suite.
- `reports/`: dated results, each generated by code.
- `docs/research/source-videos.md`: every rule from the source videos and the claims under test.
- `deploy/`: 24/7 server setup (Google Cloud free tier).
- `data/README.md`: every dataset, where it comes from and how to get it. Data files themselves aren't committed.
- `CLAUDE.md`: ground rules, facts, hypotheses and milestones.
