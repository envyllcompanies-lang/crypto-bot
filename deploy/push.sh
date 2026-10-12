#!/usr/bin/env bash
# Commit files to the paper-results branch on GitHub with the deploy key (see README).
# Usage: push.sh <folder on the branch> <commit message> <file>...
# Does nothing unless CRYPTOBOT_PUSH_REPORTS=1 is in .env. Files are copied as they are,
# so pass only reports: never .env, keys or raw data.
set -euo pipefail
[ "${CRYPTOBOT_PUSH_REPORTS:-0}" = "1" ] || exit 0
dest="$1" message="$2"
shift 2

DIR="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
remote="$(git -C "$DIR" remote get-url origin)"
if ! git clone -q --depth 1 -b paper-results "$remote" "$work" 2>/dev/null; then
  rm -rf "$work" && mkdir -p "$work"
  git -C "$work" init -q
  git -C "$work" checkout -q -b paper-results
fi
mkdir -p "$work/$dest"
for f in "$@"; do
  [ -f "$f" ] && cp "$f" "$work/$dest/"
done
git -C "$work" add "$dest"
if git -C "$work" -c user.name="crypto-bot server" \
    -c user.email="crypto-bot-server@users.noreply.github.com" \
    commit -q -m "$message"; then
  git -C "$work" push -q "$remote" HEAD:paper-results
  echo "pushed $dest/ to the paper-results branch"
fi
