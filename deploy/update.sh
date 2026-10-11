#!/usr/bin/env bash
# Every 15 minutes (crypto-bot-update.timer): if GitHub has new commits on this branch,
# pull them, install the pinned packages and run the tests. The paper trader restarts
# only if the tests pass; otherwise the code goes back to the version that was running.
# A restart starts a new paper-trading run.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
UV="$HOME/.local/bin/uv"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

git fetch -q origin "$BRANCH"
OLD="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
if [ "$OLD" = "$NEW" ]; then
  exit 0
fi

echo "updating ${OLD:0:7} -> ${NEW:0:7}"
git merge -q --ff-only "origin/$BRANCH"
if "$UV" sync --frozen && "$UV" run --frozen pytest -q; then
  bash deploy/install-units.sh
  sudo systemctl restart crypto-bot.service
  echo "updated to ${NEW:0:7} and restarted the paper trader"
else
  echo "tests failed on ${NEW:0:7}; staying on ${OLD:0:7}"
  git reset -q --hard "$OLD"
  "$UV" sync --frozen
  exit 1
fi
