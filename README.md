# crypto-bot

A backtester for a Pump.fun dip-buying strategy taken from YouTube videos, followed later by a paper trader. It does not trade with real money.

## Trade size

The starting wallet is 1 SOL: 0.25 SOL per New Pairs trade and 0.4 SOL per Final Stretch trade. Stakes can grow with the wallet, but no single trade is ever more than 1 SOL, and only once the wallet passes a threshold the owner sets. The cap may rise to 1.5 SOL later, never higher.

- `CLAUDE.md`: ground rules, facts, hypotheses and milestones.
- `docs/research/source-videos.md`: every rule from the source videos and the claims under test.
