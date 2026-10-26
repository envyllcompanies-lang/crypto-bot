#!/usr/bin/env bash
# Start the Slinky21 backtest (cryptobot.backtest) in the background, as its own systemd
# unit capped at 450 MB and low priority, so the paper trader keeps its memory and CPU.
# It survives logging out. Arguments go to the backtest, e.g. --from 2026-09-25.
# One Polars thread and two malloc arenas keep it near 380 MB (measured on 7 Oct data).
#   follow it:  journalctl -u crypto-bot-backtest -f
#   stop it:    sudo systemctl stop crypto-bot-backtest
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
if systemctl is-active --quiet crypto-bot-backtest; then
  echo "a backtest is already running: journalctl -u crypto-bot-backtest -f" >&2
  exit 1
fi
sudo systemctl reset-failed crypto-bot-backtest 2>/dev/null || true
sudo systemd-run --unit=crypto-bot-backtest --uid="$(id -u)" --gid="$(id -g)" \
  -p WorkingDirectory="$DIR" -p MemoryMax=450M -p MemorySwapMax=0 -p Nice=15 \
  -p Environment="PYTHONUNBUFFERED=1 POLARS_MAX_THREADS=1 MALLOC_ARENA_MAX=2" \
  "$DIR/.venv/bin/python" -m cryptobot.backtest "$@"
echo "started; follow it with: journalctl -u crypto-bot-backtest -f"
