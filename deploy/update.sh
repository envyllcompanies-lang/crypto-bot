#!/usr/bin/env bash
# Every 15 minutes (crypto-bot-update.timer): if GitHub has new commits on this branch,
# pull them, install the pinned packages and run the tests. If they pass, restart only
# the services whose code the commits changed (cryptobot.affected): a commit touching
# docs, tests or reports restarts nothing, and a fix to the X reader doesn't restart the
# paper trader (each paper-trader restart starts a new paper-trading run). A service
# you stopped stays stopped. If the tests fail, or a restarted service doesn't stay up,
# the code goes back to the version that was running and that commit isn't tried again.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
UV="$HOME/.local/bin/uv"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
FAILED="data/derived/update_failed"  # the last commit that failed, not to be retried

timeout 120 git fetch -q origin "$BRANCH"
OLD="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
if [ "$OLD" = "$NEW" ]; then
  exit 0
fi
if [ -f "$FAILED" ] && [ "$(cat "$FAILED")" = "$NEW" ]; then
  exit 0  # already tried and failed; wait for a newer commit
fi

back_to_old() {
  echo "$NEW" > "$FAILED"
  git reset -q --hard "$OLD"
  "$UV" sync --frozen
}

echo "updating ${OLD:0:7} -> ${NEW:0:7}"
if git merge-base --is-ancestor "$OLD" "$NEW"; then
  git merge -q --ff-only "origin/$BRANCH"
else  # the branch was rewritten (rebase, force-push): follow it rather than stall
  echo "origin/$BRANCH no longer contains ${OLD:0:7}; moving to ${NEW:0:7}"
  git reset -q --hard "$NEW"
fi

mkdir -p data/derived
if ! { "$UV" sync --frozen && timeout 600 "$UV" run --frozen pytest -q; }; then
  echo "tests failed on ${NEW:0:7}; staying on ${OLD:0:7} (not retrying this commit)"
  back_to_old
  exit 1
fi
rm -f "$FAILED"

bash deploy/install-units.sh --refresh
mapfile -t UNITS < <("$UV" run --frozen python -m cryptobot.affected "$OLD" "$NEW")
if [ "${#UNITS[@]}" -eq 0 ]; then
  echo "updated to ${NEW:0:7}; nothing that runs changed, no restart"
  exit 0
fi
# Only units that are running now are restarted: a service you stopped stays stopped.
RUNNING=()
for unit in "${UNITS[@]}"; do
  if systemctl is-active -q "$unit"; then RUNNING+=("$unit"); fi
done
if [ "${#RUNNING[@]}" -eq 0 ]; then
  echo "updated to ${NEW:0:7}; ${UNITS[*]} not running, nothing restarted"
  exit 0
fi
restarts() { systemctl show -p NRestarts --value "$1"; }
sudo systemctl restart "${RUNNING[@]}"
declare -A BEFORE  # read after the restart: a manual restart may reset the counter
for unit in "${RUNNING[@]}"; do BEFORE[$unit]="$(restarts "$unit")"; done
sleep 45
BROKEN=()
for unit in "${RUNNING[@]}"; do
  # down, or crash-looping (systemd restarted it on its own since)
  if ! systemctl is-active -q "$unit" || [ "$(restarts "$unit")" != "${BEFORE[$unit]}" ]; then
    BROKEN+=("$unit")
  fi
done
if [ "${#BROKEN[@]}" -gt 0 ]; then
  echo "${BROKEN[*]} didn't stay up on ${NEW:0:7}; going back to ${OLD:0:7}"
  back_to_old
  bash deploy/install-units.sh --refresh
  sudo systemctl restart "${RUNNING[@]}"
  exit 1
fi
echo "updated to ${NEW:0:7}; restarted ${RUNNING[*]}"
