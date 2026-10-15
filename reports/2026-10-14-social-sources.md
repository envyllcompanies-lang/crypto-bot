# Social, narrative and coin-validity sources: what other bots use, and what we added

14 Oct 2026. These findings come from three parallel web searches of GitHub and the open web. Nothing found was downloaded, installed or run. Stars and free-tier limits come from fetched pages or search snippets and were not verified against live accounts; a live check here is marked.

## What open-source memecoin bots use for social and narrative data

- **X (Twitter).** Every bot that reads X does one of two things.
  - Pays for the official API: elizaOS's Twitter plugin now uses API keys, and X has been pay-per-use since February 2026.
  - Scrapes with logged-in accounts (twscrape, twikit). This is against X's terms. twikit has been broken upstream since March 2026, snscrape and Twint are dead, and Nitter shut down after a cease-and-desist.
  - No free, permitted way to read X at scale exists.
- **Telegram call channels.** Telethon user accounts can read them, but:
  - accounts on unofficial clients get flagged and banned;
  - Telegram's terms added a data-scraping ban in February 2026;
  - Telethon moved off GitHub.
  - Grey area, so we skip it unless the owner decides otherwise.
- **Pump.fun comments and livestreams.** Only through the site's own undocumented frontend API. That is close to our "no site automation" rule, so we avoid it.
- **AI narrative scoring.** It is common, and none of the projects found publishes evidence that it predicts outcomes. One paper-trading bot, `m3n4ce-dev/meme_traderv1`, reports the opposite in its README: over 153 calls its AI-chosen buys did 10 points worse at the median than the coins it passed on.
- **Crypto social APIs.**
  - LunarCrush, Kaito and TheTie: social data is paid.
  - Santiment: the free tier lags 30 days.
  - CryptoPanic: the free API was reportedly dropped.
- **Malware.** Fake "Solana trading bot" repos with inflated star counts have stolen keys:
  - a key stealer hidden in an npm dependency (SlowMist, July 2025);
  - key-stealing npm and PyPI packages (Socket, January 2025);
  - a hijacked GitHub organisation hosting a bot that stole wallet keys (StepSecurity, 2026).

  Stars and official-looking organisation names are weak trust signals.

## Added to `social.py` (free, official or permitted, no key)

| Source | Signal | Matched by |
|---|---|---|
| DexScreener community takeovers | a community took over an abandoned coin | mint |
| CoinGecko `/search/trending` | top-searched coins (live check: works without a key) | name or ticker |
| 4chan /biz/ catalog (official read-only API, once a minute) | mint addresses and $TICKERs mentioned; words spiking to at least 4x their own day-long baseline (`biz_spike`) | mint, ticker, name |
| Bluesky Jetstream (free, no login) | mint addresses and $TICKERs in every public post over the last hour | mint, ticker |

Already in place:
- DexScreener boosts and profiles;
- GeckoTerminal trending pools;
- Google Trends RSS;
- Wikipedia top articles;
- X oEmbed for single linked posts;
- the free-tier AI narrative judge.

Like those, the new signals are logged on every gate record and not traded on until `social.py report` shows they predict outcomes.

**Live check on /biz/.** Explicit tickers and mint addresses were rare: none in 1,029 posts. Coins were named in plain words ("pump foodpepe"), which is why word spikes were added.

## Coin validity

- **Third-party checks don't fit the curve.** Off-the-shelf safety APIs (GoPlus, TokenSniffer, De.Fi, Birdeye security) mostly check mint and freeze authority and liquidity locks. On a Pump.fun curve the program already guarantees all of those, so these APIs say little before graduation.
- **RugCheck** (`api.rugcheck.xyz`, live check):
  - For a brand-new Pump.fun coin it answered "unable to generate report".
  - For three trending older coins it gave score 1 with no risks listed.
  - Not added.
- **Bubblemaps.** Its maps refresh within hours, too slow for coins minutes old.
- **Better done in our own code from our own trade stream.** Each item below needs a ruled build first, before it can join the gate records. These ideas come from the papers and from `agiprolabs/claude-trading-skills` (MIT), which is to be read and reimplemented, never installed.
  1. **Wash-trade share.** Same-wallet round trips, A→B→A loops, and buy and sell in one transaction, as a share of volume.
  2. **Bundle supply share and how much of it has sold.** Buyers grouped by slot, by transaction, and by similar SOL amounts.
  3. **Holder funding clusters.** Extend the top-5 funding check to the top 20-30 holders, tracing funders up to 3 hops, and record each cluster's share of supply.
  4. **Creators hiding behind fresh wallets, and coordinated sells.** Flag deployers funded by wallets that funded earlier launches, and several top holders selling in the same few slots.
  5. **Offline check against a labelled set.** Validate our filters against MELT's labelled set of 41k migrated launches (`git-disl/MELT`, CC BY-NC 4.0, non-commercial).

## Would need the owner's keys or money

| Source | Cost | Notes |
|---|---|---|
| Reddit API (PRAW) | free | New apps need manual approval, reportedly 2-4 weeks |
| CoinGecko Demo key | free | Higher limits |
| YouTube Data API | free | 10,000 units a day |
| Helius | free tier | Faster, fuller on-chain data and funding traces |
| Elfa AI | 1,000 free credits, then paid | Trending contract addresses on X and Telegram |
| X API | paid | — |
