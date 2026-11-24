"""Offline demo: the cost report on the recorded fixture, then a mock replay.

Runs with no key, no network and no installed packages:

    python demo/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from costlab.cli import main  # noqa: E402


def section(title: str) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)


if __name__ == "__main__":
    section("1. Recorded usage, costed under every strategy (baseline: Opus tier)")
    code = main(["report"])
    print()
    section("2. Same workload, cheaper tier set to the smallest model")
    code = code or main(["report", "--cheaper", "claude-haiku-5-5"])
    print()
    section("3. Replay runner, offline mock transport")
    code = code or main(["replay"])
    raise SystemExit(code)
