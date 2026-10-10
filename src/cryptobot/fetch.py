"""Download dataset files from Hugging Face into data/raw/.

Usage: uv run python -m cryptobot.fetch <dataset> [<path-prefix> ...]

Files land in data/raw/<owner>__<name>/<path>. Existing files with the right size
are skipped. Downloaded files are data only: never import or execute them.
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
RAW = Path(__file__).resolve().parents[2] / "data" / "raw"


def list_files(dataset: str, prefix: str = "") -> list[dict]:
    url = f"{HF}/api/datasets/{dataset}/tree/main/{prefix}?recursive=true"
    with urllib.request.urlopen(url, timeout=60) as resp:
        entries = json.load(resp)
    return [e for e in entries if e.get("type") == "file"]


def local_dir(dataset: str) -> Path:
    return RAW / dataset.replace("/", "__")


def download(dataset: str, path: str, size: int, attempts: int = 8) -> Path:
    """Download one file, resuming with HTTP Range requests when a transfer is cut."""
    dest = local_dir(dataset) / path
    if dest.exists() and dest.stat().st_size == size:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = f"{HF}/datasets/{dataset}/resolve/main/{path}"
    for _ in range(attempts):
        have = tmp.stat().st_size if tmp.exists() else 0
        if have == size:
            break
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                # A server that ignores Range sends the whole file again.
                mode = "ab" if have and resp.status == 206 else "wb"
                with tmp.open(mode) as out:
                    shutil.copyfileobj(resp, out, length=1 << 20)
        except OSError:
            continue
    if not tmp.exists() or tmp.stat().st_size != size:
        got = tmp.stat().st_size if tmp.exists() else 0
        raise OSError(f"{path}: got {got} bytes, expected {size}")
    tmp.rename(dest)
    return dest


def main(argv: list[str]) -> None:
    dataset, prefixes = argv[0], argv[1:] or [""]
    files = [f for p in prefixes for f in list_files(dataset, p)]
    total = sum(f["size"] for f in files)
    print(f"{dataset}: {len(files)} files, {total / 1e9:.2f} GB")
    for f in files:
        download(dataset, f["path"], f["size"])
        print(f"  {f['path']}")


if __name__ == "__main__":
    main(sys.argv[1:])
