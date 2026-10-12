#!/usr/bin/env bash
# Every hour (crypto-bot-snapshot.timer): the paper report so far, the labels report and a
# short health status, pushed to latest/ on the paper-results branch when
# CRYPTOBOT_PUSH_REPORTS=1, so they can be read on GitHub (or by Claude) without logging in.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
PYTHON="$DIR/.venv/bin/python"
OUT="data/reports/latest"
mkdir -p "$OUT"

"$PYTHON" -m cryptobot.m6_report > "$OUT/paper.md" || rm -f "$OUT/paper.md"
"$PYTHON" -m cryptobot.labels --out data/derived/labels-latest.parquet > "$OUT/labels.md" \
  || rm -f "$OUT/labels.md"
{
  echo "# Server status"
  echo
  echo "- Time (UTC): $(date -u '+%Y-%m-%d %H:%M')"
  echo "- Paper trader: $(systemctl is-active crypto-bot || true)"
  echo "- Running since: $(systemctl show -p ActiveEnterTimestamp --value crypto-bot || true)"
  echo "- Code: $(git log --oneline -1)"
  echo "- Disk: $(df -h / | awk 'NR==2 {print $3 " used of " $2 ", " $4 " free"}')"
  echo "- Memory: $(free -m | awk 'NR==2 {print $3 " MB used of " $2 " MB"}')"
  echo "- Restarts of the paper trader: $(systemctl show -p NRestarts --value crypto-bot || true)"
} > "$OUT/status.md"

bash "$DIR/deploy/push.sh" latest "Snapshot $(date -u '+%Y-%m-%d %H:%M')" \
  "$OUT/paper.md" "$OUT/labels.md" "$OUT/status.md"
