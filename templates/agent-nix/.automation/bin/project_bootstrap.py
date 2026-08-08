#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PLACEHOLDER = "@@PROJECT_NAME@@"
SKIP_DIRS = {".git", ".direnv", ".venv", ".worktrees", ".task-state", "target", ".build", "build"}
TEXT_SUFFIXES = {"", ".md", ".toml", ".txt", ".nix", ".json", ".cmake"}


class BootstrapError(RuntimeError):
    pass


def repository_root() -> Path:
    return Path.cwd().resolve()


def normalize_name(raw: str) -> str:
    value = raw.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        raise BootstrapError("project name normalizes to an empty value")
    if value[0].isdigit():
        value = "project-" + value
    return value


def candidate_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"CMakeLists.txt", "README.md"}:
            continue
        result.append(path)
    return sorted(result)


def bootstrap(root: Path, requested_name: str | None) -> dict:
    name = normalize_name(requested_name or root.name)
    changed: list[str] = []
    remaining: list[str] = []
    for path in candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if PLACEHOLDER not in text:
            continue
        updated = text.replace(PLACEHOLDER, name)
        path.write_text(updated, encoding="utf-8")
        changed.append(path.relative_to(root).as_posix())

    for path in candidate_files(root):
        try:
            if PLACEHOLDER in path.read_text(encoding="utf-8"):
                remaining.append(path.relative_to(root).as_posix())
        except UnicodeDecodeError:
            continue
    if remaining:
        raise BootstrapError("unresolved project-name placeholders remain: " + ", ".join(remaining))
    return {"status": "PASS", "projectName": name, "changedPaths": changed, "idempotent": not changed}


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time generated project bootstrap")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    try:
        print(json.dumps(bootstrap(repository_root(), args.name), sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
