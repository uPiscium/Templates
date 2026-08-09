#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def root() -> Path:
    return Path.cwd().resolve()


def sanitized_environment(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(repo / ".venv")
    env["PIP_REQUIRE_VIRTUALENV"] = "1"
    entries = []
    for entry in env.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry).expanduser()
        if path.name == "bin" and path.parent.name == ".venv":
            continue
        entries.append(entry)
    env["PATH"] = os.pathsep.join(entries)
    return env


def report(repo: Path) -> dict:
    env = sanitized_environment(repo)
    inherited = {
        "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV"),
        "UV_PROJECT_ENVIRONMENT": os.environ.get("UV_PROJECT_ENVIRONMENT"),
    }
    return {
        "repositoryRoot": str(repo),
        "inherited": inherited,
        "resolved": {
            "VIRTUAL_ENV": None,
            "UV_PROJECT_ENVIRONMENT": env["UV_PROJECT_ENVIRONMENT"],
            "PIP_REQUIRE_VIRTUALENV": env["PIP_REQUIRE_VIRTUALENV"],
        },
        "crossWorktreeVenvRemovedFromPath": all(
            not (Path(entry).name == "bin" and Path(entry).parent.name == ".venv")
            for entry in env.get("PATH", "").split(os.pathsep)
            if entry
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Python Project Adapter worktree-local environment boundary")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report")
    execute = sub.add_parser("exec")
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    repo = root()
    if args.command == "report":
        print(json.dumps(report(repo), sort_keys=True))
        return 0
    if not args.argv:
        parser.error("exec requires a command")
    return subprocess.run(args.argv, cwd=repo, env=sanitized_environment(repo)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
