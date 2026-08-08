#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


class UpgradeError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UpgradeError(f"{' '.join(command)}: {detail}")
    return result


def root() -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    return Path(result.stdout.strip()).resolve()


def version(repo: Path) -> str:
    path = repo / ".automation" / "VERSION"
    if not path.is_file():
        raise UpgradeError("missing .automation/VERSION")
    return path.read_text(encoding="utf-8").strip()


def upstream(repo: Path) -> dict[str, str]:
    path = repo / ".automation" / "UPSTREAM"
    if not path.is_file():
        raise UpgradeError("missing .automation/UPSTREAM")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    required = ("repository", "ref", "component")
    missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key]]
    if missing:
        raise UpgradeError("invalid UPSTREAM fields: " + ", ".join(missing))
    return {key: raw[key] for key in required}


def context(repo: Path) -> dict:
    return {
        "version": version(repo),
        "adapter": (repo / ".automation" / "ADAPTER").read_text(encoding="utf-8").strip(),
        "upstream": upstream(repo),
    }


def resolve_source(path: Path | None) -> Path:
    if path is None:
        raise UpgradeError("update check requires --source <Templates checkout>; no remote code is fetched automatically")
    source = path.resolve()
    if not (source / "components" / "agent-core" / ".automation" / "VERSION").is_file():
        raise UpgradeError(f"not a Templates source checkout: {source}")
    return source


def plan(repo: Path, source: Path) -> dict:
    local = version(repo)
    remote = version(source / "components" / "agent-core")
    return {
        "currentVersion": local,
        "upstreamVersion": remote,
        "updateAvailable": local != remote,
        "source": str(source),
        "managedPaths": [".automation/**", ".opencode/**", "AGENTS.md", "Justfile", "opencode.json"],
        "protectedRepositoryPaths": ["just/project/**", "just/local.just", ".github/workflows/**"],
        "readOnly": True,
    }


def require_maintenance(repo: Path) -> None:
    branch = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
    if not branch:
        raise UpgradeError("detached HEAD is not supported")
    default = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo, check=False).stdout.strip().removeprefix("origin/")
    if default and branch == default:
        raise UpgradeError("upgrade refused on default branch")
    if not (repo / ".task-state" / "task.md").is_file():
        raise UpgradeError("upgrade requires a Task worktree with Task State")
    if os.environ.get("AUTOMATION_MAINTENANCE") != "1":
        raise UpgradeError("upgrade requires AUTOMATION_MAINTENANCE=1 in a dedicated Automation Maintenance Task")


def managed(relative: Path) -> bool:
    text = relative.as_posix()
    if text in {"AGENTS.md", "Justfile", "opencode.json"}:
        return True
    if text.startswith(".opencode/"):
        return True
    if text.startswith(".automation/"):
        return text not in {
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
        }
    return False


def apply(repo: Path, source: Path) -> dict:
    require_maintenance(repo)
    source_core = source / "components" / "agent-core"
    changed: list[str] = []
    for path in sorted(source_core.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_core)
        if not managed(relative):
            continue
        destination = repo / relative
        before = destination.read_bytes() if destination.is_file() else None
        data = path.read_bytes()
        if before == data:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        changed.append(relative.as_posix())
    return {
        "status": "APPLIED",
        "repositoryRoot": str(repo),
        "sourceCore": str(source_core),
        "adapter": (repo / ".automation" / "ADAPTER").read_text(encoding="utf-8").strip(),
        "changedPaths": changed,
        "commitCreated": False,
        "pushPerformed": False,
        "mergePerformed": False,
        "requiredNextChecks": ["git diff --check", "just agent::doctor", "just project::check", "repository CI/smoke tests"],
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agent Core version/update/upgrade contract")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    check = sub.add_parser("check-update")
    check.add_argument("--source", type=Path, required=True)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--source", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        repo = root()
        if args.command == "version":
            result = context(repo)
        elif args.command == "check-update":
            result = plan(repo, resolve_source(args.source))
        elif args.command == "upgrade":
            result = apply(repo, resolve_source(args.source))
        else:  # pragma: no cover
            raise UpgradeError(f"unsupported command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except UpgradeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
