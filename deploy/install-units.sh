#!/usr/bin/env bash
# Install or refresh the systemd units for this checkout and enable the timers.
# Called by setup.sh and update.sh.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
for unit in crypto-bot.service crypto-bot-daily.service crypto-bot-daily.timer \
    crypto-bot-update.service crypto-bot-update.timer; do
  sed -e "s|@USER@|$USER|g" -e "s|@DIR@|$DIR|g" "$DIR/deploy/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable -q --now crypto-bot-daily.timer crypto-bot-update.timer
sudo systemctl enable -q crypto-bot.service
