"""Which long-running services a commit changes, so an update restarts only those.

Each restart of the paper trader starts a new paper-trading run (Final Stretch waits 40
minutes again, open positions are closed and flagged), so a commit that only touches
docs, tests or reports must not restart it, and a change to the X reader must not
restart the paper trader either.

A service is affected when the commit changes any module it can import (the closure of
`import cryptobot...` / `from cryptobot... import`, including imports inside functions),
its unit file, or the locked dependencies.

Usage (deploy/update.sh): python -m cryptobot.affected OLD NEW   -> unit names, one a line
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
SERVICES = {  # unit -> the module it runs (ExecStart in deploy/*.service)
    "crypto-bot.service": "paper",
    "crypto-bot-social.service": "social",
    "crypto-bot-x.service": "xscrape",
}
EVERYONE = ("pyproject.toml", "uv.lock")


def imports(path: Path) -> set[str]:
    """The cryptobot modules a file imports anywhere in it."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("cryptobot."):
                    found.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "cryptobot":
                found.update(alias.name for alias in node.names)
            elif node.module.startswith("cryptobot."):
                found.add(node.module.split(".")[1])
    return found


def closure(module: str, src: Path = SRC) -> set[str]:
    """`module` and every cryptobot module it can reach."""
    seen, todo = set(), [module]
    while todo:
        m = todo.pop()
        if m in seen or not (src / f"{m}.py").exists():
            continue
        seen.add(m)
        todo.extend(imports(src / f"{m}.py") - seen)
    return seen


def affected(changed: list[str], src: Path = SRC) -> list[str]:
    """Units to restart for these changed paths (relative to the repo root)."""
    changed_modules = {
        Path(p).stem for p in changed if p.startswith("src/cryptobot/") and p.endswith(".py")
    }
    out = []
    for unit, module in SERVICES.items():
        if (
            any(p in EVERYONE for p in changed)
            or f"deploy/{unit}" in changed
            or changed_modules & closure(module, src)
        ):
            out.append(unit)
    return out


def changed_paths(old: str, new: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", old, new], capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> None:
    old, new = (argv or sys.argv[1:])[:2]
    for unit in affected(changed_paths(old, new)):
        print(unit)


if __name__ == "__main__":
    main()
