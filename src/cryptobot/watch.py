"""The paper trader's watchlist: coins it wants a closer look at, one JSON line each
(written by paper.Engine, read by social.py and xscrape.py). Its own small module so
the X service can read it without loading the whole social collector."""

from __future__ import annotations

import json
from pathlib import Path

from cryptobot.recorder import LIVE

WATCH_PATH = LIVE.parent / "derived" / "watch.jsonl"


class Watchlist:
    """New coins in a JSON-lines file (each line has a "mint"), each returned once.

    Coins already in the file when the reader starts count as seen, so only coins added
    later are returned, even after daily.sh rewrites the file shorter (`tail -n 5000`):
    the reader then reads it again from the top and skips what it has seen. Only
    complete lines are read; a line still being written waits for the next call."""

    def __init__(self, path: Path = WATCH_PATH) -> None:
        self.path = path
        self.offset = 0
        self.done: set[str] = set()
        self.new()  # what is there already is not new

    def new(self) -> list[dict]:
        if not self.path.exists():
            return []
        rotated = self.path.stat().st_size < self.offset
        if rotated:
            self.offset = 0
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # complete lines only
        self.offset += end
        seen_now: set[str] = set()
        out = []
        for line in data[:end].splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            mint = item.get("mint") if isinstance(item, dict) else None
            if not isinstance(mint, str) or not mint:
                continue
            seen_now.add(mint)
            if mint not in self.done:
                self.done.add(mint)
                out.append(item)
        if rotated:  # the file now holds the recent coins only: so does `done`
            self.done = seen_now
        return out
