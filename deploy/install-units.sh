#!/usr/bin/env bash
# Install or refresh the systemd units for this checkout and enable the timers.
# Called by setup.sh and update.sh.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
for unit in crypto-bot.service crypto-bot-social.service crypto-bot-x.service \
    crypto-bot-daily.service crypto-bot-daily.timer \
    crypto-bot-snapshot.service crypto-bot-snapshot.timer \
    crypto-bot-update.service crypto-bot-update.timer; do
  sed -e "s|@USER@|$USER|g" -e "s|@DIR@|$DIR|g" "$DIR/deploy/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
# A 1 GB swap file, once: with no swap, running out of memory freezes the whole machine.
if ! swapon --show=NAME --noheadings | grep -q . && [ ! -f /swapfile ]; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
swapon --show=NAME --noheadings | grep -q . || sudo swapon /swapfile || true
sudo systemctl daemon-reload
sudo systemctl enable -q --now crypto-bot-daily.timer crypto-bot-update.timer crypto-bot-snapshot.timer
sudo systemctl enable -q crypto-bot.service
sudo systemctl enable -q --now crypto-bot-social.service
# Idle (it checks every 10 minutes) until the owner runs `xscrape login`.
sudo systemctl enable -q --now crypto-bot-x.service
