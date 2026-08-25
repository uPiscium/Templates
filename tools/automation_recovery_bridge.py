#!/usr/bin/env python3
"""Narrow Templates-root bridge for source-side maintenance recovery."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent.parent
ENGINE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"


class BridgeError(RuntimeError):
    pass


def load_engine() -> ModuleType:
    """Load the repository's supported recovery implementation, not a substitute."""
    spec = None
    try:
        spec = importlib.util.spec_from_file_location("templates_automation_upgrade", ENGINE_PATH)
        if spec is None or spec.loader is None:
            raise BridgeError(f"cannot create import specification for supported engine: {ENGINE_PATH}")
        engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = engine
        spec.loader.exec_module(engine)
        return engine
    except BridgeError:
        raise
    except Exception as exc:
        if spec is not None and spec.name in sys.modules:
            del sys.modules[spec.name]
        raise BridgeError(f"cannot load supported recovery engine {ENGINE_PATH}: {exc}") from exc


def require_templates_root(engine: ModuleType) -> Path:
    try:
        top = Path(engine.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT).stdout.strip()).resolve()
    except Exception as exc:
        if isinstance(exc, engine.UpgradeError):
            raise
        raise BridgeError(f"cannot verify Templates Git root: {exc}") from exc
    if top != ROOT:
        raise BridgeError(f"recovery bridge must run from the Templates Git root: {top}")
    return top


@contextmanager
def maintenance_environment():
    previous = os.environ.get("AUTOMATION_MAINTENANCE")
    os.environ["AUTOMATION_MAINTENANCE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AUTOMATION_MAINTENANCE", None)
        else:
            os.environ["AUTOMATION_MAINTENANCE"] = previous


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Templates source-side maintenance recovery bridge")
    sub = result.add_subparsers(dest="command", required=True)
    recover = sub.add_parser("recover-maintenance-authority")
    recover.add_argument("target", type=Path)
    commit = sub.add_parser("commit-recovered-maintenance")
    commit.add_argument("target", type=Path)
    commit.add_argument("task")
    commit.add_argument("message", nargs="?", default="")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        engine = load_engine()
    except BridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        root = require_templates_root(engine)
        target = args.target.resolve()
        with maintenance_environment():
            if args.command == "recover-maintenance-authority":
                result = engine.recover_maintenance_authority_from_source(target, root)
            elif args.command == "commit-recovered-maintenance":
                result = engine.commit_recovered_maintenance(target, root, args.task, args.message)
            else:  # pragma: no cover
                raise BridgeError(f"unsupported bridge command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except engine.UpgradeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except BridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
