# Strategy research: what the evidence supports (14 Oct 2026)

Why: every dip-buying variant lost money after fees, in the backtest and in 70 hours of
live paper trading (reports/2026-10-14-live-review.md). This note collects the best
published evidence on other approaches, to choose what to test next.

**How it was gathered.** Three web searches, run in parallel. None of the searches
could open full texts (the sites failed to resolve), so every outside figure below
comes from abstracts, search snippets or press coverage. Check a figure in the paper
before it goes into any decision. Figures from our own data are marked **(ours)** and
come from code in this repo.

## The main findings

1. **Nobody has published a working minute-scale memecoin strategy.** No paper tests
   momentum or reversal on Pump.fun or DEX microcaps at minute-to-hour horizons. The
   only positive net figure found is for copy trading: about +3% per trade for a
   copier after frictions (Luo et al., WWW 2026, arXiv:2601.08641). A reviewer says
   bonding-curve costs were not fully modelled, so treat +3% as an upper bound.
2. **Manipulation is the norm among "winners".**
   - About 17% of Pump.fun trades are wash trades, and wash trading goes with up to 10x
     higher graduation odds (Szwajcok et al., CMU/EPFL, arXiv:2609.10246).
   - 82.8% of memecoins that rose more than 100% show artificial growth (Mongardini &
     Mei, USENIX Security 2026).
   - The wallets that look best on paper are often the manipulators. This hurts copy
     trading most.
3. **Skill persists, but at second-scale horizons.**
   - On Hyperliquid, wallet rank by short-horizon edge keeps a 0.52 correlation from
     one 10-day window to the next (Zhai, arXiv:2608.04373).
   - On Polymarket, 44% of "skilled" traders stay skilled out of sample (Gómez-Cram et
     al., CEPR DP21615).
   - No study tests persistence for Solana memecoin wallets.
   - Following a known caller loses: influencer-promoted coins gain about +1.8% on day
     one, then reverse to -6.5% over 30 days (Merkley et al., Review of Accounting
     Studies 2024). In Telegram pumps, people arriving seconds late are the exit
     liquidity.
4. **After graduation, most tokens collapse within minutes (ours: `uv run python -m cryptobot.screen --pools`).**
   - Source: loopholetape `graduation_pools`, 12,975 complete pools that opened
     between 23 Sep and 3 Oct.
   - The median price 30 minutes after the pool opens is 3.4% of its opening price.
   - 70.8% of pools are below half their opening price; 23.7% are above it.
   - 8,499 of 13,051 pools fell to 10% of their opening price within 30 minutes, with
     a median of 363 s.
   - Pump.fun's BOOST (since 21 Jul 2026, per The Block) adds a known buy of about
     17.6 SOL over the first 5 minutes after migration. That is mechanical flow, but
     the collapse above happens with it in place.
5. **Attention signals are plausible but unmeasured for Pump.fun.** Attention predicts
   weekly returns of large coins (Liu & Tsyvinski, RFS 2021). The Telegram-link
   "lift" for Pump.fun graduation comes from Kamat (arXiv:2607.02823), whose estimates
   are withdrawn. Do not use it.
6. **Rug detection is useful as a veto, not as an edge.** The best reported model reaches
   a PR-AUC of about 0.76 from the first 5 minutes of trades (Li et al.,
   arXiv:2608.20271). Its authors call it not good enough to deploy.

## What this means for our tests

| Idea | Evidence | Test here |
|---|---|---|
| Momentum entries on the curve | None published | `screen.py`, `mom_*` entries |
| Ride the last stretch to graduation, sell at 95% | None; buying *after* graduation is clearly bad (finding 4) | `screen.py`, `late_*` entries with `ride95` exits |
| Copy wallets, ranked by what a copier would have made | Luo et al. +3% at most; skill persists elsewhere | `follow.py`: `copyable` list from 1-23 Sep, judged from 24 Sep |
| Copy only when two separate leaders buy | Recommended against bait wallets | `follow.py`, `copy_two_*` |
| Rug-risk veto on any entry | PR-AUC ~0.76 | Later, if an entry rule shows promise |
| Post-graduation trading | Collapse is the base case | Not worth the 277 GB download for now |

## Withdrawn or unreliable sources not to use

- **Kamat:** arXiv:2607.02795 (sniper rings) and the survival, graduation-rate and
  Telegram-lift claims in arXiv:2607.02823. Both are withdrawn.
- **Vendor figures:** "93% of top wallets are bots", post-migration survival rates and
  deployer graduation multiples. None of these come with a method.

## Sources

- Luo, Feng, Xu, Liu, "Resisting Manipulative Bots in Meme Coin Copy Trading", WWW 2026, https://arxiv.org/abs/2601.08641
- Szwajcok et al., "Meme Coin Factories", https://arxiv.org/abs/2609.10246
- Mongardini & Mei, "A Midsummer Meme's Dream", USENIX Security 2026, https://arxiv.org/abs/2507.01963
- Zhai, "Public Trader Identity", https://arxiv.org/abs/2608.04373
- Gómez-Cram, Guo, Kung, Jensen, CEPR DP21615, https://cepr.org/publications/dp21615
- Merkley, Pacelli, Piorkowski, Williams, "Crypto-influencers", Review of Accounting Studies 2024
- Marino, Naviglio, Tarantelli, Lillo, "Predicting the success of new crypto-tokens: the Pump.fun case", https://arxiv.org/abs/2602.14860
- MemeTrans / MELT, https://arxiv.org/abs/2602.13480
- Li et al., "Catching the Rug", https://arxiv.org/abs/2608.20271
- Liu & Tsyvinski, "Risks and Returns of Cryptocurrency", RFS 2021
- The Block on BOOST, https://www.theblock.co/post/409815/pump-fun-token-graduation-rate-jumps-boost-changes-launch-incentives
- loopholetape/pumpfun-launches (CC BY 4.0), `graduation_pools` config
