from __future__ import annotations

import subprocess
from pathlib import Path

from crypto_trader.research.kronos.pins import KRONOS_CODE_COMMIT, KRONOS_CODE_REPOSITORY

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "artifacts" / "Kronos-source"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    if not SOURCE.exists():
        run("git", "clone", "--filter=blob:none", "--no-checkout", KRONOS_CODE_REPOSITORY, str(SOURCE))
    run("git", "-C", str(SOURCE), "fetch", "origin", KRONOS_CODE_COMMIT)
    run("git", "-C", str(SOURCE), "switch", "--detach", KRONOS_CODE_COMMIT)
    actual = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if actual != KRONOS_CODE_COMMIT:
        raise RuntimeError(f"Kronos source pin mismatch: expected {KRONOS_CODE_COMMIT}, got {actual}")
    print(actual)


if __name__ == "__main__":
    main()
