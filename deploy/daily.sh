#!/usr/bin/env bash
# Once a day (crypto-bot-daily.timer): delete raw data older than CRYPTOBOT_KEEP_DAYS
# (default 14), rebuild the nightly lists, write the reports, and, if
# CRYPTOBOT_PUSH_REPORTS=1 is in .env, commit the reports to the paper-results branch on
# GitHub with the deploy key. Cleanup runs first, so a failing report can never leave the
# disk to fill up, and each report that fails is skipped, not the rest of the night.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
PYTHON="$DIR/.venv/bin/python"
KEEP_DAYS="${CRYPTOBOT_KEEP_DAYS:-14}"
DAY="$(date -u +%Y-%m-%d)"
if ! [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  echo "CRYPTOBOT_KEEP_DAYS must be a whole number of days, not '$KEEP_DAYS'; using 14" >&2
  KEEP_DAYS=14
fi

# Cleanup. Raw events are needed only to replay a run: data/paper/<run>/events/<table>/
# <date>; social snapshots: data/live/social/<source>/<date>; a standalone recorder's
# tables: data/live/<table>/<date>.
prune() {  # prune <root> <depth>: date folders older than KEEP_DAYS at that depth
  [ -d "$1" ] || return 0
  find "$1" -mindepth "$2" -maxdepth "$2" -type d -name '20??-??-??' -mtime +"$KEEP_DAYS" \
    -exec rm -rf {} + || echo "cleanup of $1 failed" >&2
}
prune data/paper 4
prune data/live/social 2
prune data/live 2
# Keep the watchlist and the X results short; watch.Watchlist notices the rewrite.
for f in data/derived/watch.jsonl data/derived/x_scrape.jsonl; do
  if [ -f "$f" ]; then
    tail -n 5000 "$f" > "$f.tmp"
    mv "$f.tmp" "$f"
  fi
done

mkdir -p data/reports
REPORTS=()
# report <file> <description> <command...>: run it into <file>; on failure, say so and
# drop the file (the previous nightly list, if any, stays in use).
report() {
  local out="$1" what="$2"
  shift 2
  if nice -n 10 "$@" > "$out"; then
    REPORTS+=("$out")
  else
    echo "$what: failed, skipped" >&2
    rm -f "$out"
  fi
}

# Tracked wallets and creator records first: the paper trader picks up new files within
# a minute.
report "data/reports/$DAY-tracked.md" "tracked wallets" \
  "$PYTHON" -m cryptobot.tracked --days "${CRYPTOBOT_TRACKED_DAYS:-1}"
report "data/reports/$DAY-creators.md" "creator records" \
  "$PYTHON" -m cryptobot.creators --days "${CRYPTOBOT_CREATOR_DAYS:-14}"
report "data/reports/$DAY-labels.md" "labels" "$PYTHON" -m cryptobot.labels
report "data/reports/$DAY-paper.md" "paper report" "$PYTHON" -m cryptobot.m6_report
report "data/reports/$DAY-review.md" "review" "$PYTHON" -m cryptobot.review
report "data/reports/$DAY-preregistered.md" "pre-registered tests" \
  "$PYTHON" -m cryptobot.prereg
report "data/reports/$DAY-social.md" "social report" "$PYTHON" -m cryptobot.social report

if [ "${#REPORTS[@]}" -gt 0 ]; then
  bash "$DIR/deploy/push.sh" reports "Paper trading report $DAY" "${REPORTS[@]}"
fi
