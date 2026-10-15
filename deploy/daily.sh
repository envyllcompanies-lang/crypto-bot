#!/usr/bin/env bash
# Once a day (crypto-bot-daily.timer): write the paper-trading report, delete raw events
# older than CRYPTOBOT_KEEP_DAYS (default 14), and, if CRYPTOBOT_PUSH_REPORTS=1 is in
# .env, commit the report to the paper-results branch on GitHub with the deploy key.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
PYTHON="$DIR/.venv/bin/python"
KEEP_DAYS="${CRYPTOBOT_KEEP_DAYS:-14}"
DAY="$(date -u +%Y-%m-%d)"

mkdir -p data/reports
# Rebuild the tracked-wallet list from recent recordings. The paper trader picks up the
# new file within a minute. A failure keeps yesterday's list and the day goes on.
TRACKED_REPORT="data/reports/$DAY-tracked.md"
if ! nice -n 10 "$PYTHON" -m cryptobot.tracked --days "${CRYPTOBOT_TRACKED_DAYS:-1}" \
    > "$TRACKED_REPORT"; then
  echo "tracked wallets: build failed, keeping the previous list" >&2
  rm -f "$TRACKED_REPORT"
fi

# Rebuild creator records (launches and graduates per creator) the same way.
CREATORS_REPORT="data/reports/$DAY-creators.md"
if ! nice -n 10 "$PYTHON" -m cryptobot.creators --days "${CRYPTOBOT_CREATOR_DAYS:-14}" \
    > "$CREATORS_REPORT"; then
  echo "creator records: build failed, keeping the previous records" >&2
  rm -f "$CREATORS_REPORT"
fi

# Labelled outcomes (every eligible token's features and result) for the adaptive layer.
LABELS_REPORT="data/reports/$DAY-labels.md"
if ! nice -n 10 "$PYTHON" -m cryptobot.labels > "$LABELS_REPORT"; then
  echo "labels: no labelled outcomes yet" >&2
  rm -f "$LABELS_REPORT"
fi

REPORT="data/reports/$DAY-paper.md"
"$PYTHON" -m cryptobot.m6_report > "$REPORT"
echo "report: $REPORT"

# Raw events are needed only to replay a run. Layout: data/paper/<run>/events/<table>/<date>.
find data/paper -mindepth 4 -maxdepth 4 -type d -path '*/events/*' -name '20??-??-??' \
  -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true

# Social snapshots (social.py): data/live/social/<source>/<date>.
find data/live/social -mindepth 2 -maxdepth 2 -type d -name '20??-??-??' \
  -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true

# Keep the watchlist short; social.Watchlist notices the rewrite and starts over.
if [ -f data/derived/watch.jsonl ]; then
  tail -n 5000 data/derived/watch.jsonl > data/derived/watch.jsonl.tmp
  mv data/derived/watch.jsonl.tmp data/derived/watch.jsonl
fi

SOCIAL_REPORT="data/reports/$DAY-social.md"
nice -n 10 "$PYTHON" -m cryptobot.social report > "$SOCIAL_REPORT" || rm -f "$SOCIAL_REPORT"

bash "$DIR/deploy/push.sh" reports "Paper trading report $DAY" \
  "$REPORT" "$TRACKED_REPORT" "$CREATORS_REPORT" "$LABELS_REPORT" "$SOCIAL_REPORT"
