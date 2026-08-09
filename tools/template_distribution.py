#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IGNORED_DIRECTORY_NAMES = {"__pycache__", ".git"}
IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}


class DistributionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    template: str
    owner: str
    repository: str
    default_branch: str
    description: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DistributionError(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DistributionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DistributionError(f"manifest root must be an object: {path}")
    return data


def load_template_names(root: Path) -> set[str]:
    data = _read_json(root / "templates" / "manifest.json")
    templates = data.get("templates")
    if set(data) != {"templates"} or not isinstance(templates, dict):
        raise DistributionError("templates/manifest.json must contain exactly 'templates'")
    return set(templates)


def load_distribution(root: Path) -> tuple[dict[str, Target], dict[str, str]]:
    data = _read_json(root / "distribution" / "template-repositories.json")
    if set(data) != {"version", "owner", "templates"}:
        raise DistributionError("distribution manifest must contain version, owner, templates")
    if data["version"] != 1:
        raise DistributionError(f"unsupported distribution manifest version: {data['version']!r}")
    owner = data["owner"]
    if not isinstance(owner, str) or not NAME_RE.fullmatch(owner):
        raise DistributionError(f"invalid distribution owner: {owner!r}")
    raw_templates = data["templates"]
    if not isinstance(raw_templates, dict):
        raise DistributionError("distribution templates must be an object")

    targets: dict[str, Target] = {}
    excluded: dict[str, str] = {}
    repositories: set[str] = set()
    for template, raw in raw_templates.items():
        if not isinstance(template, str) or not NAME_RE.fullmatch(template):
            raise DistributionError(f"invalid template key: {template!r}")
        if not isinstance(raw, dict) or not isinstance(raw.get("published"), bool):
            raise DistributionError(f"template {template!r} requires boolean published")
        if raw["published"]:
            expected = {"published", "repository", "default_branch", "description"}
            if set(raw) != expected:
                raise DistributionError(
                    f"published template {template!r} must contain exactly {sorted(expected)}"
                )
            repository = raw["repository"]
            branch = raw["default_branch"]
            description = raw["description"]
            if not isinstance(repository, str) or not NAME_RE.fullmatch(repository):
                raise DistributionError(f"invalid repository for {template!r}: {repository!r}")
            if not isinstance(branch, str) or not NAME_RE.fullmatch(branch):
                raise DistributionError(f"invalid default branch for {template!r}: {branch!r}")
            if not isinstance(description, str) or not description.strip():
                raise DistributionError(f"missing description for {template!r}")
            full_name = f"{owner}/{repository}".lower()
            if full_name in repositories:
                raise DistributionError(f"duplicate target repository: {owner}/{repository}")
            repositories.add(full_name)
            targets[template] = Target(template, owner, repository, branch, description)
        else:
            if set(raw) != {"published", "reason"}:
                raise DistributionError(
                    f"unpublished template {template!r} must contain exactly published and reason"
                )
            reason = raw["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise DistributionError(f"unpublished template {template!r} requires a reason")
            excluded[template] = reason

    registered = load_template_names(root)
    configured = set(raw_templates)
    if registered != configured:
        missing = sorted(registered - configured)
        extra = sorted(configured - registered)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unknown: " + ", ".join(extra))
        raise DistributionError("distribution/template manifest mismatch: " + "; ".join(details))
    for template in registered:
        if not (root / "templates" / template).is_dir():
            raise DistributionError(f"missing generated template directory: templates/{template}")
    return targets, excluded


def target_for(root: Path, template: str) -> Target:
    targets, excluded = load_distribution(root)
    if template in excluded:
        raise DistributionError(f"template {template!r} is not published: {excluded[template]}")
    try:
        return targets[template]
    except KeyError as exc:
        raise DistributionError(f"unknown template: {template!r}") from exc


def _iter_files(root: Path):
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(d for d in directories if d not in IGNORED_DIRECTORY_NAMES)
        files.sort()
        symlink_dirs = [d for d in directories if (current_path / d).is_symlink()]
        for directory in symlink_dirs:
            path = current_path / directory
            yield path, path.relative_to(root), "symlink"
            directories.remove(directory)
        for filename in files:
            path = current_path / filename
            if path.suffix in IGNORED_FILE_SUFFIXES:
                continue
            yield path, path.relative_to(root), "symlink" if path.is_symlink() else "file"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root: Path) -> dict[str, tuple[str, str, bool]]:
    if not root.is_dir():
        return {}
    result: dict[str, tuple[str, str, bool]] = {}
    for source, relative, kind in _iter_files(root):
        key = relative.as_posix()
        if kind == "symlink":
            result[key] = ("symlink", os.readlink(source), False)
        else:
            executable = bool(stat.S_IMODE(source.stat().st_mode) & 0o111)
            result[key] = ("file", _digest(source), executable)
    return result


def diff(expected: dict, actual: dict) -> list[str]:
    differences: list[str] = []
    expected_keys = set(expected)
    actual_keys = set(actual)
    for path in sorted(expected_keys - actual_keys):
        differences.append(f"missing: {path}")
    for path in sorted(actual_keys - expected_keys):
        differences.append(f"unexpected: {path}")
    for path in sorted(expected_keys & actual_keys):
        if expected[path] != actual[path]:
            differences.append(f"changed: {path}")
    return differences


def _safe_destination(root: Path, destination: Path) -> Path:
    resolved = destination.resolve()
    if resolved in {root.resolve(), root.resolve().parent, Path("/")}:
        raise DistributionError(f"refusing unsafe destination: {resolved}")
    if not (resolved / ".git").exists():
        raise DistributionError(f"destination is not a checked-out Git repository: {resolved}")
    return resolved


def materialize(root: Path, template: str, destination: Path) -> None:
    target_for(root, template)
    source = root / "templates" / template
    destination = _safe_destination(root, destination)

    for child in destination.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)

    for source_path, relative, kind in _iter_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            os.symlink(os.readlink(source_path), target)
        else:
            shutil.copy2(source_path, target, follow_symlinks=False)


def check(root: Path, template: str, destination: Path) -> list[str]:
    target_for(root, template)
    destination = _safe_destination(root, destination)
    return diff(snapshot(root / "templates" / template), snapshot(destination))


def command_verify(root: Path) -> int:
    targets, excluded = load_distribution(root)
    for name in sorted(targets):
        target = targets[name]
        print(f"publish: {name} -> {target.full_name}:{target.default_branch}")
    for name in sorted(excluded):
        print(f"excluded: {name} — {excluded[name]}")
    return 0


def command_matrix(root: Path) -> int:
    targets, _ = load_distribution(root)
    include = [
        {
            "template": target.template,
            "repository": target.full_name,
            "default_branch": target.default_branch,
        }
        for target in sorted(targets.values(), key=lambda item: item.template)
    ]
    print(json.dumps({"include": include}, separators=(",", ":"), sort_keys=True))
    return 0


def command_materialize(root: Path, template: str, destination: str) -> int:
    materialize(root, template, Path(destination))
    print(f"materialized: {template} -> {Path(destination).resolve()}")
    return 0


def command_check(root: Path, template: str, destination: str) -> int:
    differences = check(root, template, Path(destination))
    if differences:
        print(f"distribution drift: {template}", file=sys.stderr)
        for difference in differences:
            print(f"  - {difference}", file=sys.stderr)
        return 1
    print(f"distribution match: {template}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage generated GitHub template repository distribution")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify")
    subparsers.add_parser("matrix")
    materialize_parser = subparsers.add_parser("materialize")
    materialize_parser.add_argument("template")
    materialize_parser.add_argument("destination")
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("template")
    check_parser.add_argument("destination")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    try:
        if args.command == "verify":
            return command_verify(root)
        if args.command == "matrix":
            return command_matrix(root)
        if args.command == "materialize":
            return command_materialize(root, args.template, args.destination)
        if args.command == "check":
            return command_check(root, args.template, args.destination)
        raise AssertionError(args.command)
    except DistributionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
