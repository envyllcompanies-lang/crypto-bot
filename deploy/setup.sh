#!/usr/bin/env bash
# Set up the 24/7 paper trader on an Ubuntu server (written for the Google Cloud
# free-tier e2-micro). Run it again any time to update the code and restart. See deploy/README.md.
#
# Paper trading only: nothing here builds, signs or sends a transaction, and no key is
# needed. The only credential involved is the GitHub deploy key you created to clone.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="$(git -C "$DIR" rev-parse --abbrev-ref HEAD)"

echo "== system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates >/dev/null

echo "== uv"
if [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$HOME/.local/bin/uv"

echo "== code ($BRANCH)"
git -C "$DIR" pull -q --ff-only origin "$BRANCH"
cd "$DIR"
"$UV" sync --frozen
"$UV" run --frozen ruff check .
"$UV" run --frozen pytest -q

echo "== services"
bash deploy/install-units.sh
sudo systemctl restart crypto-bot.service
sleep 10
systemctl --no-pager --lines=5 status crypto-bot.service || true

echo
echo "Paper trader running. Logs:   journalctl -u crypto-bot -f"
echo "Report so far:  cd $DIR && $UV run python -m cryptobot.m6_report"
