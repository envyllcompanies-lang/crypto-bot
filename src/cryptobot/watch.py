"""The paper trader's watchlist: coins it wants a closer look at, one JSON line each
(written by paper.Engine, read by social.py and xscrape.py). Its own small module so
the X service can read it without loading the whole social collector."""

from __future__ import annotations

import json
from pathlib import Path

from cryptobot.recorder import LIVE

WATCH_PATH = LIVE.parent / "derived" / "watch.jsonl"


class Watchlist:
    """New lines of the paper trader's watch file, read from where it last stopped."""

    def __init__(self, path: Path = WATCH_PATH) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0  # only new coins
        self.done: set[str] = set()

    def new(self) -> list[dict]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # the file was rotated
            self.offset = 0
        with self.path.open() as f:
            f.seek(self.offset)
            lines = f.readlines()
            self.offset = f.tell()
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("mint") and item["mint"] not in self.done:
                self.done.add(item["mint"])
                out.append(item)
        return out
