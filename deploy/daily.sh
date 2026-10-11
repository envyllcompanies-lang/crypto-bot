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
REPORT="data/reports/$DAY-paper.md"
"$PYTHON" -m cryptobot.m6_report > "$REPORT"
echo "report: $REPORT"

# Raw events are needed only to replay a run. Layout: data/paper/<run>/events/<table>/<date>.
find data/paper -mindepth 4 -maxdepth 4 -type d -path '*/events/*' -name '20??-??-??' \
  -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true

if [ "${CRYPTOBOT_PUSH_REPORTS:-0}" = "1" ]; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  remote="$(git remote get-url origin)"
  if ! git clone -q --depth 1 -b paper-results "$remote" "$work" 2>/dev/null; then
    rm -rf "$work" && mkdir -p "$work"
    git -C "$work" init -q
    git -C "$work" checkout -q -b paper-results
  fi
  mkdir -p "$work/reports"
  cp "$REPORT" "$work/reports/"
  git -C "$work" add reports
  if git -C "$work" -c user.name="crypto-bot server" \
      -c user.email="crypto-bot-server@users.noreply.github.com" \
      commit -q -m "Paper trading report $DAY"; then
    git -C "$work" push -q "$remote" HEAD:paper-results
    echo "pushed reports/$DAY-paper.md to the paper-results branch"
  fi
fi
