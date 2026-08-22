#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from environment import sanitized_environment


def ensure_lockfile(
    repo: Path,
    lockfile: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> int:
    path = repo / lockfile
    if path.exists():
        if path.is_file():
            return 0
        print(f"lockfile path is not a file: {lockfile}", file=sys.stderr)
        return 1

    if env is None:
        result = subprocess.run(command, cwd=repo)
    else:
        result = subprocess.run(command, cwd=repo, env=env)
    if result.returncode != 0:
        return result.returncode
    if not path.is_file():
        print(f"lockfile generation did not create {lockfile}", file=sys.stderr)
        return 1
    return 0


def ensure(repo: Path) -> int:
    result = ensure_lockfile(repo, "flake.lock", ["nix", "flake", "lock"])
    if result != 0:
        return result
    return ensure_lockfile(
        repo,
        "uv.lock",
        ["uv", "lock"],
        env=sanitized_environment(repo),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize missing Python project lockfiles")
    parser.add_argument("command", choices=["ensure"])
    args = parser.parse_args()
    if args.command == "ensure":
        return ensure(Path.cwd().resolve())
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
