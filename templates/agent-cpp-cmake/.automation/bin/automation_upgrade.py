#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class UpgradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Action:
    path: str
    action: str
    reason: str


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    remove_paths: tuple[Path, ...]
    require_absent_paths: tuple[Path, ...]


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


def load_ownership(repo: Path) -> dict[str, str]:
    path = repo / ".automation" / "ownership.toml"
    if not path.is_file():
        return {}
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in raw.get("paths", {}).items()}


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


def deletable_managed(relative: Path) -> bool:
    text = relative.as_posix()
    if text == "opencode.json" or text.startswith(".opencode/"):
        return True
    if text.startswith(".automation/"):
        return text not in {
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
            ".automation/VERSION",
        }
    return False


def version_number(repo: Path) -> int:
    raw = version(repo)
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise UpgradeError(f"invalid Agent Core VERSION: {raw!r}") from exc
    if parsed <= 0 or str(parsed) != raw:
        raise UpgradeError(f"invalid Agent Core VERSION: {raw!r}")
    return parsed


def migration_path(raw: object, *, field: str, managed_delete: bool) -> Path:
    if not isinstance(raw, str) or not raw:
        raise UpgradeError(f"migrations.toml {field} entries must be non-empty strings")
    if "\\" in raw or any(character in raw for character in "*?[]{}"):
        raise UpgradeError(f"migrations.toml {field} path must be exact repository-relative: {raw!r}")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise UpgradeError(f"migrations.toml {field} path is unsafe: {raw!r}")
    if pure.as_posix() != raw:
        raise UpgradeError(f"migrations.toml {field} path is not normalized: {raw!r}")
    relative = Path(*pure.parts)
    if managed_delete and not deletable_managed(relative):
        raise UpgradeError(f"migrations.toml remove_paths path is not Agent Core managed: {raw!r}")
    return relative


def load_migrations(source_core: Path) -> list[Migration]:
    path = source_core / ".automation" / "migrations.toml"
    if not path.is_file():
        return []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpgradeError(f"invalid migrations.toml: {exc}") from exc
    unknown_top_level = set(raw) - {"schema_version", "migrations"}
    if unknown_top_level:
        raise UpgradeError(f"migrations.toml unknown top-level fields: {sorted(unknown_top_level)}")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise UpgradeError("migrations.toml schema_version must be integer 1")
    entries = raw.get("migrations", [])
    if not isinstance(entries, list):
        raise UpgradeError("migrations.toml migrations must be an array of tables")

    migrations: list[Migration] = []
    transitions: set[tuple[int, int]] = set()
    required_fields = {"from_version", "to_version", "remove_paths", "require_absent_paths"}
    for index, entry in enumerate(entries):
        label = f"migrations.toml migrations[{index}]"
        if not isinstance(entry, dict):
            raise UpgradeError(f"{label} must be a table")
        unknown = set(entry) - required_fields
        missing = required_fields - set(entry)
        if unknown:
            raise UpgradeError(f"{label} unknown fields: {sorted(unknown)}")
        if missing:
            raise UpgradeError(f"{label} missing fields: {sorted(missing)}")
        from_version = entry["from_version"]
        to_version = entry["to_version"]
        if type(from_version) is not int or type(to_version) is not int or from_version <= 0 or to_version <= 0:
            raise UpgradeError(f"{label} versions must be positive integers")
        if to_version != from_version + 1:
            raise UpgradeError(f"{label} transition must advance exactly one version")
        transition = (from_version, to_version)
        if transition in transitions:
            raise UpgradeError(f"{label} duplicates transition {from_version} -> {to_version}")
        transitions.add(transition)

        parsed_paths: dict[str, tuple[Path, ...]] = {}
        for field, managed_delete in (("remove_paths", True), ("require_absent_paths", False)):
            values = entry[field]
            if not isinstance(values, list):
                raise UpgradeError(f"{label}.{field} must be a list")
            paths = tuple(
                migration_path(value, field=f"{label}.{field}", managed_delete=managed_delete)
                for value in values
            )
            if len(set(paths)) != len(paths):
                raise UpgradeError(f"{label}.{field} contains duplicate paths")
            parsed_paths[field] = paths
        migrations.append(
            Migration(
                from_version=from_version,
                to_version=to_version,
                remove_paths=parsed_paths["remove_paths"],
                require_absent_paths=parsed_paths["require_absent_paths"],
            )
        )
    return sorted(migrations, key=lambda item: (item.to_version, item.from_version))


def path_present(path: Path) -> bool:
    return os.path.lexists(path)


def symlink_ancestor(repo: Path, relative: Path) -> Path | None:
    current = repo
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            return current.relative_to(repo)
    return None


def migration_actions(
    repo: Path,
    migrations: list[Migration],
) -> tuple[list[Action], list[str], set[Path]]:
    actions: list[Action] = []
    blockers: list[str] = []
    deleted: set[Path] = set()
    for migration in migrations:
        transition = f"{migration.from_version} -> {migration.to_version}"
        for relative in migration.require_absent_paths:
            if relative in deleted:
                continue
            ancestor = symlink_ancestor(repo, relative)
            if ancestor is not None:
                blockers.append(
                    f"migration {transition} precondition {relative.as_posix()}: "
                    f"symlink ancestor {ancestor.as_posix()} is unsafe; operator must remove or replace it"
                )
            elif path_present(repo / relative):
                blockers.append(
                    f"migration {transition} precondition {relative.as_posix()}: path must be absent; "
                    "operator must resolve it before upgrading"
                )
        for relative in migration.remove_paths:
            ancestor = symlink_ancestor(repo, relative)
            if ancestor is not None:
                actions.append(
                    Action(
                        relative.as_posix(),
                        "blocked",
                        f"migration {transition} delete has symlink ancestor {ancestor.as_posix()}",
                    )
                )
                continue
            destination = repo / relative
            if relative in deleted:
                continue
            if not path_present(destination):
                actions.append(
                    Action(
                        relative.as_posix(),
                        "noop",
                        f"already absent for Agent Core migration {transition}",
                    )
                )
                continue
            if destination.is_symlink() or destination.is_file():
                actions.append(
                    Action(
                        relative.as_posix(),
                        "delete",
                        f"removed by Agent Core migration {transition}",
                    )
                )
                deleted.add(relative)
            elif destination.is_dir():
                actions.append(Action(relative.as_posix(), "blocked", f"migration {transition} refuses directory deletion"))
            else:
                actions.append(Action(relative.as_posix(), "blocked", f"migration {transition} refuses special-file deletion"))
    return actions, blockers, deleted


def replace_agent_rules(existing: str, upstream_rules: str) -> tuple[str | None, str]:
    begin = "<!-- BEGIN AGENT CORE RULES -->"
    end = "<!-- END AGENT CORE RULES -->"
    begin_count = existing.count(begin)
    end_count = existing.count(end)
    if begin_count != 1 or end_count != 1:
        return None, "managed Agent Core rules block is missing or malformed"
    start = existing.index(begin)
    finish = existing.index(end, start) + len(end)
    replacement = f"{begin}\n{upstream_rules.rstrip()}\n{end}"
    return existing[:start] + replacement + existing[finish:], "replace managed Agent Core rules block"


def merge_just_router(existing: str, upstream: str) -> tuple[str | None, str]:
    module_re = re.compile(r"^(mod\??)\s+([A-Za-z0-9_-]+)\s+(['\"])(.+?)\3\s*$")
    required: dict[str, str] = {}
    for line in upstream.splitlines():
        match = module_re.match(line.strip())
        if match:
            required[match.group(2)] = line.strip()
    lines = existing.splitlines()
    by_name: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        match = module_re.match(line.strip())
        if match:
            by_name.setdefault(match.group(2), []).append(index)
    for name, expected in required.items():
        indices = by_name.get(name, [])
        if len(indices) > 1:
            return None, f"Just module {name!r} is declared more than once"
        if indices:
            current = lines[indices[0]].strip()
            current_match = module_re.match(current)
            expected_match = module_re.match(expected)
            assert current_match and expected_match
            if current_match.group(4) != expected_match.group(4):
                return None, f"Just module {name!r} points to repository-owned path {current_match.group(4)!r}"
            lines[indices[0]] = expected
        else:
            lines.append(expected)
    suffix = "\n" if existing.endswith("\n") or not existing else ""
    return "\n".join(lines) + suffix, "merge Agent Core router declarations"


def write_topology_blocker(repo: Path, relative: Path, planned_deletes: set[Path]) -> str | None:
    current = repo
    current_relative = Path()
    for part in relative.parts[:-1]:
        current /= part
        current_relative /= part
        if current_relative in planned_deletes:
            return None
        if current.is_symlink():
            return f"symlink ancestor {current_relative.as_posix()} cannot be upgraded safely"
        if path_present(current) and not current.is_dir():
            return f"non-directory ancestor {current_relative.as_posix()} blocks managed path"
    destination = repo / relative
    if relative not in planned_deletes and destination.is_symlink():
        return "destination symlink cannot be upgraded safely"
    return None


def action_for(repo: Path, source_core: Path, relative: Path, ownership: dict[str, str]) -> Action:
    rel = relative.as_posix()
    destination = repo / relative
    source = source_core / relative
    if not destination.exists():
        return Action(rel, "create", "Agent Core-owned path is absent")
    if not destination.is_file() or not source.is_file():
        return Action(rel, "blocked", "non-file collision cannot be upgraded safely")
    if destination.read_bytes() == source.read_bytes():
        return Action(rel, "noop", "already matches upstream")

    if rel == "AGENTS.md":
        existing = destination.read_text(encoding="utf-8")
        has_marker = "<!-- BEGIN AGENT CORE RULES -->" in existing or "<!-- END AGENT CORE RULES -->" in existing
        if has_marker:
            merged, detail = replace_agent_rules(existing, source.read_text(encoding="utf-8"))
            return Action(rel, "merge" if merged is not None else "blocked", detail)
        if ownership.get(rel) == "replace":
            return Action(rel, "replace", "ownership metadata marks generated AGENTS.md as replaceable")
        return Action(rel, "blocked", "AGENTS.md ownership is ambiguous and no managed block exists")

    if rel == "Justfile":
        existing = destination.read_text(encoding="utf-8")
        adopted_router = "# Agent Core module router" in existing
        if adopted_router or ownership.get(rel) != "replace":
            merged, detail = merge_just_router(existing, source.read_text(encoding="utf-8"))
            return Action(rel, "merge" if merged is not None else "blocked", detail)
        return Action(rel, "replace", "ownership metadata marks generated Justfile as replaceable")

    return Action(rel, "replace", "Agent Core-owned path")


def build_plan(repo: Path, source: Path) -> dict:
    local = version(repo)
    remote = version(source / "components" / "agent-core")
    local_number = version_number(repo)
    source_core = source / "components" / "agent-core"
    remote_number = version_number(source_core)
    ownership = load_ownership(repo)
    migrations = load_migrations(source_core)
    selected = [
        migration
        for migration in migrations
        if migration.to_version <= remote_number
    ]
    actions, blockers, deleted = migration_actions(repo, selected)
    if remote_number < local_number:
        blockers.append(
            f"source Agent Core VERSION {remote_number} is older than local VERSION {local_number}; downgrade is forbidden"
        )
    for path in sorted(source_core.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_core)
        if managed(relative):
            topology_blocker = write_topology_blocker(repo, relative, deleted)
            if topology_blocker:
                actions.append(Action(relative.as_posix(), "blocked", topology_blocker))
            elif relative in deleted:
                actions.append(Action(relative.as_posix(), "create", "reintroduced after an Agent Core migration"))
            else:
                actions.append(action_for(repo, source_core, relative, ownership))
    blockers.extend(f"{item.path}: {item.reason}" for item in actions if item.action == "blocked")
    changed = [item.path for item in actions if item.action in {"create", "replace", "merge", "delete"}]
    return {
        "currentVersion": local,
        "upstreamVersion": remote,
        "updateAvailable": bool(changed),
        "source": str(source),
        "managedPaths": [".automation/**", ".opencode/**", "AGENTS.md", "Justfile", "opencode.json"],
        "protectedRepositoryPaths": ["just/project/**", "just/local.just", ".github/workflows/**"],
        "actions": [asdict(item) for item in actions],
        "blockers": blockers,
        "canApply": not blockers,
        "readOnly": True,
    }


def require_maintenance(repo: Path) -> None:
    branch = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
    if not branch:
        raise UpgradeError("detached HEAD is not supported")
    default = run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        check=False,
    ).stdout.strip().removeprefix("origin/")
    if default and branch == default:
        raise UpgradeError("upgrade refused on default branch")
    if not (repo / ".task-state" / "task.md").is_file():
        raise UpgradeError("upgrade requires a Task worktree with Task State")
    if os.environ.get("AUTOMATION_MAINTENANCE") != "1":
        raise UpgradeError("upgrade requires AUTOMATION_MAINTENANCE=1 in a dedicated Automation Maintenance Task")


def apply(repo: Path, source: Path) -> dict:
    require_maintenance(repo)
    plan = build_plan(repo, source)
    if plan["blockers"]:
        raise UpgradeError("upgrade blocked:\n- " + "\n- ".join(plan["blockers"]))
    source_core = source / "components" / "agent-core"
    changed: list[str] = []
    actionable = [item for item in plan["actions"] if item["action"] != "noop"]
    planned_deletes = {Path(item["path"]) for item in actionable if item["action"] == "delete"}
    preflight_blockers: list[str] = []
    for item in actionable:
        relative = Path(item["path"])
        destination = repo / relative
        if item["action"] == "delete":
            if path_present(destination) and not (destination.is_symlink() or destination.is_file()):
                preflight_blockers.append(f"{item['path']}: delete target changed to an unsafe type")
        else:
            blocker = write_topology_blocker(repo, relative, planned_deletes)
            if blocker:
                preflight_blockers.append(f"{item['path']}: {blocker}")
    if preflight_blockers:
        raise UpgradeError("upgrade blocked before mutation:\n- " + "\n- ".join(preflight_blockers))

    version_actions = [item for item in actionable if item["path"] == ".automation/VERSION"]
    ordinary_actions = [item for item in actionable if item["path"] != ".automation/VERSION"]
    phases = {"delete": 0, "create": 1, "replace": 2, "merge": 3}
    ordered_actions = sorted(
        ordinary_actions,
        key=lambda item: (phases.get(item["action"], 99), item["path"]),
    ) + version_actions
    for item in ordered_actions:
        relative = Path(item["path"])
        source_path = source_core / relative
        destination = repo / relative
        if item["action"] == "delete":
            ancestor = symlink_ancestor(repo, relative)
            if ancestor is not None:
                raise UpgradeError(
                    f"delete path gained symlink ancestor after planning: {ancestor.as_posix()}"
                )
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif path_present(destination):
                raise UpgradeError(f"delete became unsafe after planning: {item['path']}")
            changed.append(item["path"])
            continue
        topology_blocker = write_topology_blocker(repo, relative, set())
        if topology_blocker:
            raise UpgradeError(f"managed path became unsafe after planning: {item['path']}: {topology_blocker}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item["action"] in {"create", "replace"}:
            if destination.is_symlink():
                raise UpgradeError(f"destination became a symlink after planning: {item['path']}")
            shutil.copy2(source_path, destination)
        elif item["action"] == "merge" and item["path"] == "AGENTS.md":
            if destination.is_symlink():
                raise UpgradeError("AGENTS.md became a symlink after planning")
            merged, detail = replace_agent_rules(
                destination.read_text(encoding="utf-8"), source_path.read_text(encoding="utf-8")
            )
            if merged is None:
                raise UpgradeError(f"AGENTS.md merge became unsafe: {detail}")
            destination.write_text(merged, encoding="utf-8")
        elif item["action"] == "merge" and item["path"] == "Justfile":
            if destination.is_symlink():
                raise UpgradeError("Justfile became a symlink after planning")
            merged, detail = merge_just_router(
                destination.read_text(encoding="utf-8"), source_path.read_text(encoding="utf-8")
            )
            if merged is None:
                raise UpgradeError(f"Justfile merge became unsafe: {detail}")
            destination.write_text(merged, encoding="utf-8")
        else:
            raise UpgradeError(f"unsupported upgrade action: {item}")
        changed.append(item["path"])
    return {
        "status": "APPLIED",
        "repositoryRoot": str(repo),
        "sourceCore": str(source_core),
        "adapter": (repo / ".automation" / "ADAPTER").read_text(encoding="utf-8").strip(),
        "changedPaths": changed,
        "commitCreated": False,
        "pushPerformed": False,
        "mergePerformed": False,
        "requiredNextChecks": [
            "git diff --check",
            "just agent::doctor",
            "just project::check",
            "repository CI/smoke tests",
        ],
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
            result = build_plan(repo, resolve_source(args.source))
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
