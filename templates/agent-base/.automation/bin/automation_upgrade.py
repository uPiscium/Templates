#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tomllib
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class UpgradeError(RuntimeError):
    pass


RECEIPT_NAME = "automation-maintenance.json"
CONSUMED_RECEIPT_NAME = "automation-maintenance.consumed.json"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "task_id",
    "branch",
    "worktree",
    "source",
    "source_revision",
    "current_version",
    "upstream_version",
    "changed_paths",
    "authority_head",
    "authority_nonce",
    "path_fingerprints",
}
_GIT_EXECUTABLE: Path | None = None


def git_head(repo: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=repo, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def source_revision(source: Path) -> str | None:
    value = git_head(source)
    return value or None


def receipt_path(repo: Path) -> Path:
    return repo / ".task-state" / RECEIPT_NAME


def consumed_receipt_path(repo: Path) -> Path:
    return repo / ".task-state" / CONSUMED_RECEIPT_NAME


def common_git_dir(repo: Path) -> Path:
    value = Path(run(["git", "rev-parse", "--git-common-dir"], cwd=repo).stdout.strip())
    return value.resolve() if value.is_absolute() else (repo / value).resolve()


def authority_path(repo: Path) -> Path:
    key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
    return common_git_dir(repo) / "opencode" / "automation-maintenance" / f"{key}.json"


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def receipt_digest(receipt: dict) -> str:
    return hashlib.sha256(canonical_json(receipt)).hexdigest()


def write_authority(repo: Path, receipt: dict) -> None:
    atomic_json_write(
        authority_path(repo),
        {
            "schema_version": 1,
            "task_id": receipt["task_id"],
            "branch": receipt["branch"],
            "worktree": receipt["worktree"],
            "authority_nonce": receipt["authority_nonce"],
            "receipt_sha256": receipt_digest(receipt),
        },
    )


def validate_authority(repo: Path, receipt: dict) -> None:
    path = authority_path(repo)
    try:
        authority = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError("missing or invalid successful-upgrade authority record") from exc
    expected = {
        "schema_version": 1,
        "task_id": receipt["task_id"],
        "branch": receipt["branch"],
        "worktree": receipt["worktree"],
        "authority_nonce": receipt["authority_nonce"],
        "receipt_sha256": receipt_digest(receipt),
    }
    if authority != expected:
        raise UpgradeError("automation receipt does not match successful-upgrade authority")


def require_ignored_untracked(repo: Path, paths: tuple[Path, ...]) -> None:
    for path in paths:
        relative = path.relative_to(repo).as_posix()
        tracked = run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=repo,
            check=False,
        ).returncode == 0
        if tracked:
            raise UpgradeError(f"required Task State path is tracked: {relative}")
        ignored = run(["git", "check-ignore", "-q", relative], cwd=repo, check=False).returncode == 0
        if not ignored:
            raise UpgradeError(f"required Task State path is not ignored: {relative}")


def atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_fingerprint(repo: Path, raw_path: str) -> dict[str, object]:
    path = repo / Path(*raw_path.split("/"))
    try:
        state = path.lstat()
    except FileNotFoundError:
        return {"state": "absent", "mode": None, "content_sha256": None}
    mode = stat.S_IMODE(state.st_mode)
    if path.is_file() and not path.is_symlink():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"state": "file", "mode": mode, "content_sha256": digest}
    if path.is_symlink():
        digest = hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
        return {"state": "symlink", "mode": mode, "content_sha256": digest}
    if path.is_dir():
        return {"state": "directory", "mode": mode, "content_sha256": None}
    return {"state": "special", "mode": mode, "content_sha256": None}


def parse_task_identity(repo: Path, task: str) -> tuple[str, str, Path]:
    if not TASK_ID_RE.fullmatch(task):
        raise UpgradeError(f"invalid Task ID: {task!r}")
    state_path = repo / ".task-state" / "task.md"
    if not state_path.is_file():
        raise UpgradeError("operation requires a Task worktree with Task State")
    text = state_path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for label in ("Task ID", "Branch", "Worktree"):
        matches = re.findall(rf"(?m)^- {re.escape(label)}: (.+)$", text)
        if len(matches) != 1 or not matches[0].strip():
            raise UpgradeError(f"Task State must contain exactly one {label} identity field")
        values[label] = matches[0].strip()
    if values["Task ID"] != task:
        raise UpgradeError("Task State identity does not match requested Task")
    branch = values["Branch"]
    if not (branch.startswith(f"task/{task}-") or branch.startswith(f"fix/{task}-")):
        raise UpgradeError(f"Task State branch is not the Task branch for {task}")
    worktree = Path(values["Worktree"]).resolve()
    if worktree != repo.resolve():
        raise UpgradeError("Task State worktree does not match the current worktree")
    return task, branch, worktree


def registered_worktrees(repo: Path) -> dict[Path, tuple[str | None, str | None]]:
    output = run(["git", "worktree", "list", "--porcelain"], cwd=repo).stdout
    records: dict[Path, tuple[str | None, str | None]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current.get("worktree"):
                branch = current.get("branch", "").removeprefix("refs/heads/") or None
                records[Path(current["worktree"]).resolve()] = (branch, current.get("HEAD"))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def require_registered_task(repo: Path, task: str) -> tuple[str, str, Path]:
    _, branch, worktree = parse_task_identity(repo, task)
    registered = registered_worktrees(repo)
    actual = registered.get(worktree)
    current_branch = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
    head = git_head(repo)
    branch_oid = run(["git", "rev-parse", f"refs/heads/{branch}"], cwd=repo, check=False).stdout.strip()
    if (
        actual is None
        or current_branch != branch
        or actual[0] != branch
        or not head
        or not branch_oid
        or actual[1] != head
        or branch_oid != head
    ):
        raise UpgradeError("current Task worktree is not registered with the expected branch")
    default = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo, check=False).stdout.strip().removeprefix("origin/")
    if not default:
        raise UpgradeError("cannot resolve default branch")
    if branch == default:
        raise UpgradeError("operation refused on the default branch")
    return task, branch, worktree


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


@dataclass(frozen=True)
class SavedPath:
    kind: str
    content: bytes | str | None
    mode: int | None


def git_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "EMAIL"
    }
    if overrides:
        environment.update(overrides)
    return environment


def git_executable() -> Path:
    global _GIT_EXECUTABLE
    if _GIT_EXECUTABLE is not None:
        return _GIT_EXECUTABLE
    candidate = shutil.which("git")
    if not candidate:
        raise UpgradeError("trusted Git executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise UpgradeError("trusted Git executable is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise UpgradeError(f"Git executable is not root-owned and immutable: {executable}")
    _GIT_EXECUTABLE = executable
    return executable


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env_overrides: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    is_git = bool(command and command[0] == "git")
    environment = git_environment(env_overrides) if is_git else None
    executable_command = [str(git_executable()), *command[1:]] if is_git else command
    result = subprocess.run(
        executable_command,
        cwd=cwd,
        text=True,
        input=input_text,
        capture_output=True,
        env=environment,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UpgradeError(f"{' '.join(command)}: {detail}")
    return result


def root() -> Path:
    installed_root = Path(__file__).resolve().parents[2]
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=installed_root)
    discovered = Path(result.stdout.strip()).resolve()
    if discovered != installed_root:
        raise UpgradeError("installed Agent Core path does not match the Git worktree root")
    return installed_root


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


def git_bytes(command: list[str], *, cwd: Path) -> bytes:
    if not command or command[0] != "git":
        raise UpgradeError("byte helper only accepts Git commands")
    result = subprocess.run(
        [str(git_executable()), *command[1:]],
        cwd=cwd,
        capture_output=True,
        check=False,
        env=git_environment(),
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise UpgradeError(f"{' '.join(command)}: {detail}")
    return result.stdout


def resolve_pinned_source(path: Path) -> tuple[Path, str]:
    source = resolve_source(path)
    top = Path(run(["git", "rev-parse", "--show-toplevel"], cwd=source).stdout.strip()).resolve()
    head = git_head(source)
    if top != source or not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise UpgradeError("source must be a Git worktree root with a full non-null HEAD")
    status = git_bytes(
        ["git", "status", "--porcelain=v1", "-z", "--", "components/agent-core"], cwd=source
    )
    if status:
        raise UpgradeError("source components/agent-core must be clean")
    return source, head


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


def git_object_bytes(repo: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        [str(git_executable()), "show", f"{revision}:{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
        env=git_environment(),
    )
    if result.returncode != 0:
        raise UpgradeError(f"cannot read Git object {path}")
    return result.stdout


def materialize_tree(
    repo: Path, revision: str, destination: Path, prefix: str = "", *, surface_only: bool = False
) -> None:
    entries = git_bytes(["git", "ls-tree", "-r", "-z", revision, "--", prefix or "."], cwd=repo).split(b"\0")
    destination.mkdir(parents=True, exist_ok=True)
    prefix_bytes = (prefix.rstrip("/") + "/").encode() if prefix else b""
    for entry in entries:
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        fields = header.split()
        if prefix_bytes and not raw_path.startswith(prefix_bytes):
            raise UpgradeError("Git returned an unexpected snapshot path")
        relative_raw = raw_path[len(prefix_bytes):] if prefix_bytes else raw_path
        try:
            relative_text = relative_raw.decode("utf-8")
            relative = PurePosixPath(relative_text)
        except UnicodeDecodeError as exc:
            raise UpgradeError("snapshot contains a non-UTF-8 path") from exc
        if surface_only and not (
            relative_text in {"AGENTS.md", "Justfile", "opencode.json"}
            or relative_text.startswith(".automation/")
            or relative_text.startswith(".opencode/")
        ):
            continue
        if len(fields) != 3 or fields[0] not in {b"100644", b"100755"} or fields[1] != b"blob":
            raise UpgradeError("Agent Core snapshot contains a symlink, submodule, or special Git entry")
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != relative_text
        ):
            raise UpgradeError("snapshot contains an unsafe or unnormalized path")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_object_bytes(repo, revision, raw_path.decode("utf-8")))
        target.chmod(0o755 if fields[0] == b"100755" else 0o644)


def materialize_source_snapshot(source: Path, revision: str, temporary: Path) -> tuple[Path, Path]:
    """Reconstruct only Agent Core from a pinned source revision.

    The returned paths are (synthetic Templates root, synthetic Agent Core
    root).  In particular, callers must not use the live source checkout for
    planning or copying after this point.
    """
    snapshot_root = temporary / "source"
    source_core = snapshot_root / "components" / "agent-core"
    materialize_tree(source, revision, source_core, "components/agent-core")
    return snapshot_root, source_core


def revalidate_source(source: Path, revision: str) -> None:
    current, current_revision = resolve_pinned_source(source)
    if current != source or current_revision != revision:
        raise UpgradeError("source changed during upgrade planning or mutation")


def apply_plan_to_tree(tree: Path, source_core: Path, plan: dict) -> list[str]:
    actionable = [item for item in plan["actions"] if item["action"] != "noop"]
    planned_deletes = {Path(item["path"]) for item in actionable if item["action"] == "delete"}
    blockers: list[str] = []
    for item in actionable:
        relative = Path(item["path"])
        if item["action"] == "delete":
            target = tree / relative
            ancestor = symlink_ancestor(tree, relative)
            if ancestor is not None:
                blockers.append(f"{item['path']}: delete has symlink ancestor {ancestor.as_posix()}")
                continue
            if path_present(target) and not (target.is_symlink() or target.is_file()):
                blockers.append(f"{item['path']}: delete target changed to an unsafe type")
        else:
            blocker = write_topology_blocker(tree, relative, planned_deletes)
            if blocker:
                blockers.append(f"{item['path']}: {blocker}")
    if blockers:
        raise UpgradeError("upgrade blocked before mutation:\n- " + "\n- ".join(blockers))
    phases = {"delete": 0, "create": 1, "replace": 2, "merge": 3}
    ordered = sorted(
        [item for item in actionable if item["path"] != ".automation/VERSION"],
        key=lambda item: (phases.get(item["action"], 99), item["path"]),
    ) + [item for item in actionable if item["path"] == ".automation/VERSION"]
    changed: list[str] = []
    for item in ordered:
        relative = Path(item["path"])
        target = tree / relative
        source = source_core / relative
        if item["action"] == "delete":
            ancestor = symlink_ancestor(tree, relative)
            if ancestor is not None:
                raise UpgradeError(
                    f"delete path gained symlink ancestor after planning: {ancestor.as_posix()}"
                )
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif path_present(target):
                raise UpgradeError(f"delete became unsafe after planning: {item['path']}")
        else:
            blocker = write_topology_blocker(tree, relative, set())
            if blocker:
                raise UpgradeError(f"managed path became unsafe after planning: {item['path']}: {blocker}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise UpgradeError(f"destination became a symlink after planning: {item['path']}")
            if item["action"] in {"create", "replace"}:
                shutil.copy2(source, target)
            elif item["action"] == "merge" and item["path"] == "AGENTS.md":
                merged, detail = replace_agent_rules(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
                if merged is None:
                    raise UpgradeError(f"AGENTS.md merge became unsafe: {detail}")
                target.write_text(merged, encoding="utf-8")
            elif item["action"] == "merge" and item["path"] == "Justfile":
                merged, detail = merge_just_router(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
                if merged is None:
                    raise UpgradeError(f"Justfile merge became unsafe: {detail}")
                target.write_text(merged, encoding="utf-8")
            else:
                raise UpgradeError(f"unsupported upgrade action: {item}")
        changed.append(item["path"])
    return sorted(set(changed))


def capture_paths(tree: Path, paths: list[str]) -> dict[str, SavedPath]:
    saved: dict[str, SavedPath] = {}
    for raw_path in paths:
        target = tree / Path(*raw_path.split("/"))
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            saved[raw_path] = SavedPath("absent", None, None)
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if target.is_symlink():
            saved[raw_path] = SavedPath("symlink", os.readlink(target), mode)
        elif target.is_file():
            saved[raw_path] = SavedPath("file", target.read_bytes(), mode)
        else:
            raise UpgradeError(f"cannot snapshot unsafe upgrade destination path: {raw_path}")
    return saved


def restore_paths(tree: Path, saved: dict[str, SavedPath]) -> None:
    for raw_path, state in saved.items():
        target = tree / Path(*raw_path.split("/"))
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif path_present(target):
            raise UpgradeError(f"cannot restore upgrade destination path: {raw_path}")
        if state.kind == "absent":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if state.kind == "file" and isinstance(state.content, bytes) and state.mode is not None:
            target.write_bytes(state.content)
            target.chmod(state.mode)
        elif state.kind == "symlink" and isinstance(state.content, str):
            target.symlink_to(state.content)
        else:  # pragma: no cover - capture_paths constructs only valid states
            raise UpgradeError(f"invalid saved upgrade destination state: {raw_path}")


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


def require_maintenance(repo: Path) -> tuple[str, str, Path]:
    branch = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
    if not branch:
        raise UpgradeError("detached HEAD is not supported")
    state = repo / ".task-state" / "task.md"
    task_match = re.search(r"(?m)^- Task ID: (.+)$", state.read_text(encoding="utf-8")) if state.is_file() else None
    if not task_match:
        raise UpgradeError("upgrade requires a registered non-default Task branch")
    task_id = task_match.group(1).strip()
    identity = require_registered_task(repo, task_id)
    require_ignored_untracked(
        repo,
        (repo / ".task-state" / "task.md", receipt_path(repo), consumed_receipt_path(repo)),
    )
    if os.environ.get("AUTOMATION_MAINTENANCE") != "1":
        raise UpgradeError("upgrade requires AUTOMATION_MAINTENANCE=1 in a dedicated Automation Maintenance Task")
    return identity


def check_update(repo: Path, source_path: Path) -> dict:
    source, revision = resolve_pinned_source(source_path)
    with tempfile.TemporaryDirectory(prefix="automation-update-") as temporary:
        snapshot, _ = materialize_source_snapshot(source, revision, Path(temporary))
        plan = build_plan(repo, snapshot)
        revalidate_source(source, revision)
    plan["source"] = str(source)
    plan["sourceRevision"] = revision
    return plan


def apply(repo: Path, source_path: Path) -> dict:
    task_id, branch, worktree = require_maintenance(repo)
    source, revision = resolve_pinned_source(source_path)
    with tempfile.TemporaryDirectory(prefix="automation-upgrade-") as temporary:
        snapshot, source_core = materialize_source_snapshot(source, revision, Path(temporary))
        plan = build_plan(repo, snapshot)
        if plan["blockers"]:
            raise UpgradeError("upgrade blocked:\n- " + "\n- ".join(plan["blockers"]))
        # This check is deliberately before any destination mutation, including
        # the no-change return path.
        revalidate_source(source, revision)
        actionable = [item for item in plan["actions"] if item["action"] != "noop"]
        if not actionable:
            return {
                "status": "NO_CHANGES",
                "repositoryRoot": str(repo),
                "sourceCore": str(source / "components" / "agent-core"),
                "sourceRevision": revision,
                "adapter": (repo / ".automation" / "ADAPTER").read_text(encoding="utf-8").strip(),
                "changedPaths": [],
                "commitCreated": False,
                "pushPerformed": False,
                "mergePerformed": False,
                "requiredNextChecks": [],
            }
        expected_paths = sorted({item["path"] for item in actionable})
        saved_paths = capture_paths(repo, expected_paths)
        changed = apply_plan_to_tree(repo, source_core, plan)
        # A source race after mutation fails closed: no receipt is issued.
        try:
            revalidate_source(source, revision)
        except UpgradeError as source_error:
            try:
                restore_paths(repo, saved_paths)
            except UpgradeError as restore_error:
                raise UpgradeError(
                    f"{source_error}; destination rollback failed: {restore_error}"
                ) from restore_error
            raise
        changed_paths = sorted(set(changed))
        result = {
            "status": "APPLIED",
            "repositoryRoot": str(repo),
            "sourceCore": str(source / "components" / "agent-core"),
            "sourceRevision": revision,
            "adapter": (repo / ".automation" / "ADAPTER").read_text(encoding="utf-8").strip(),
            "changedPaths": changed_paths,
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
        receipt = {
            "schema_version": 1,
            "status": "active",
            "task_id": task_id,
            "branch": branch,
            "worktree": str(worktree),
            "source": str(source),
            "source_revision": revision,
            "current_version": plan["currentVersion"],
            "upstream_version": plan["upstreamVersion"],
            "changed_paths": changed_paths,
            "authority_head": git_head(repo),
            "authority_nonce": secrets.token_hex(32),
            "path_fingerprints": {path: file_fingerprint(repo, path) for path in changed_paths},
        }
        atomic_json_write(receipt_path(repo), receipt)
        write_authority(repo, receipt)
        consumed_receipt_path(repo).unlink(missing_ok=True)
        return result


def pending_paths(repo: Path) -> list[str]:
    paths: set[str] = set()
    for args in (
        ("diff", "--no-ext-diff", "--name-only", "-z"),
        ("diff", "--no-ext-diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        output = git_bytes(["git", *args], cwd=repo)
        for raw in output.split(b"\0"):
            if not raw:
                continue
            try:
                paths.add(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise UpgradeError("Git reported a non-UTF-8 pending path") from exc
    return sorted(paths)


def bootstrap_fingerprint(tree: Path, raw_path: str) -> dict[str, object]:
    return file_fingerprint(tree, raw_path)


def reject_bootstrap_path(repo: Path, raw_path: str) -> None:
    path = validate_receipt_path(raw_path)
    relative = Path(*path.split("/"))
    policy_path = repo / ".automation" / "policy.toml"
    policy = tomllib.loads(policy_path.read_text(encoding="utf-8")) if policy_path.is_file() else {}
    secret_patterns = policy.get("paths", {}).get("secret_patterns", [])
    if path == ".task-state" or path.startswith(".task-state/"):
        raise UpgradeError(f"pending path is Task State: {path}")
    if any(token.lower() in path.lower() for token in secret_patterns):
        raise UpgradeError(f"pending path matches a configured secret pattern: {path}")
    if path.startswith("just/project/") or path == "just/local.just":
        raise UpgradeError(f"pending path is repository-owned: {path}")
    if path.startswith(".github/"):
        raise UpgradeError(f"pending path is repository-owned: {path}")
    if path == ".automation/ADAPTER" or path == ".automation/INIT.fragment.md" or path == ".automation/adoption.toml":
        raise UpgradeError(f"pending path is Adapter-owned: {path}")
    if not managed(relative):
        raise UpgradeError(f"pending path is outside Agent Core: {path}")


def bootstrap_receipt(repo: Path, source_path: Path) -> dict:
    task_id, branch, worktree = require_maintenance(repo)
    authority_head = git_head(repo)
    authority_branch_oid = run(["git", "rev-parse", f"refs/heads/{branch}"], cwd=repo).stdout.strip()
    if receipt_path(repo).exists() or authority_path(repo).exists():
        raise UpgradeError("cannot bootstrap with an existing receipt or authority record")
    pending = pending_paths(repo)
    if not pending:
        raise UpgradeError("bootstrap requires non-empty pending paths")
    for path in pending:
        reject_bootstrap_path(repo, path)
    source, source_head = resolve_pinned_source(source_path)
    require_ignored_untracked(repo, (repo / ".task-state" / "automation-bootstrap.tmp",))
    with tempfile.TemporaryDirectory(prefix="automation-bootstrap-", dir=repo / ".task-state") as temporary:
        baseline = Path(temporary) / "baseline"
        source_snapshot = Path(temporary) / "source" / "components" / "agent-core"
        materialize_tree(repo, authority_head, baseline, surface_only=True)
        materialize_source_snapshot(source, source_head, Path(temporary))
        before = {
            p.relative_to(baseline).as_posix(): bootstrap_fingerprint(baseline, p.relative_to(baseline).as_posix())
            for p in baseline.rglob("*") if p.is_file()
        }
        plan = build_plan(baseline, source_snapshot.parent.parent)
        if plan["blockers"]:
            raise UpgradeError("upgrade blocked:\n- " + "\n- ".join(plan["blockers"]))
        selected = [
            migration for migration in load_migrations(source_snapshot)
            if migration.to_version <= version_number(source_snapshot)
        ]
        _, migration_blockers, _ = migration_actions(repo, selected)
        if migration_blockers:
            raise UpgradeError("upgrade blocked:\n- " + "\n- ".join(migration_blockers))
        apply_plan_to_tree(baseline, source_snapshot, plan)
        returned = set(item["path"] for item in plan["actions"] if item["action"] != "noop")
        expected = sorted(
            path for path in returned
            if before.get(path, {"state": "absent", "mode": None, "content_sha256": None})
            != bootstrap_fingerprint(baseline, path)
        )
        if expected != pending:
            raise UpgradeError("pending paths do not exactly match the reconstructed upgrade")
        if not expected:
            revalidate_source(source, source_head)
            return {"status": "NO_CHANGES", "changedPaths": [], "authorityIssued": False}
        expected_fingerprints = {path: bootstrap_fingerprint(baseline, path) for path in expected}
    revalidate_source(source, source_head)
    current_identity = require_registered_task(repo, task_id)
    current_registration = registered_worktrees(repo).get(worktree)
    if (
        current_identity != (task_id, branch, worktree)
        or current_registration != (branch, authority_head)
        or git_head(repo) != authority_head
        or run(["git", "rev-parse", f"refs/heads/{branch}"], cwd=repo).stdout.strip() != authority_branch_oid
    ):
        raise UpgradeError("Task identity or HEAD changed during receipt reconstruction")
    if pending_paths(repo) != pending:
        raise UpgradeError("pending paths changed during receipt reconstruction")
    current_fingerprints = {path: file_fingerprint(repo, path) for path in expected}
    if current_fingerprints != expected_fingerprints:
        raise UpgradeError("pending Agent Core content does not match the reconstructed upgrade")
    receipt = {
        "schema_version": 1, "status": "active", "task_id": task_id, "branch": branch,
        "worktree": str(worktree), "source": str(source.resolve()), "source_revision": source_head,
        "current_version": plan["currentVersion"], "upstream_version": plan["upstreamVersion"],
        "changed_paths": expected, "authority_head": authority_head, "authority_nonce": secrets.token_hex(32),
        "path_fingerprints": current_fingerprints,
    }
    atomic_json_write(receipt_path(repo), receipt)
    write_authority(repo, receipt)
    consumed_receipt_path(repo).unlink(missing_ok=True)
    return {"status": "RECEIPT_BOOTSTRAPPED", "changedPaths": expected, "authorityIssued": True}


def validate_receipt_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise UpgradeError("receipt contains an unsafe path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or pure.as_posix() != raw:
        raise UpgradeError(f"receipt contains an unnormalized path: {raw!r}")
    return raw


def receipt_paths(repo: Path, receipt: dict) -> list[str]:
    raw = receipt.get("changed_paths")
    if not isinstance(raw, list) or not raw:
        raise UpgradeError("automation receipt has no changed paths")
    paths = [validate_receipt_path(item) for item in raw]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise UpgradeError("automation receipt paths must be sorted and unique")
    secret_file = repo / ".automation" / "policy.toml"
    policy = tomllib.loads(secret_file.read_text(encoding="utf-8")) if secret_file.is_file() else {}
    secrets = policy.get("paths", {}).get("secret_patterns", [])
    for path in paths:
        relative = Path(*path.split("/"))
        if path == ".task-state" or path.startswith(".task-state/") or not managed(relative):
            raise UpgradeError(f"receipt path is not Agent Core managed: {path}")
        if any(token.lower() in path.lower() for token in secrets):
            raise UpgradeError(f"receipt path matches a configured secret pattern: {path}")
    return paths


def validate_receipt_schema(receipt: object) -> dict:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise UpgradeError("automation upgrade receipt has an invalid schema")
    if receipt.get("schema_version") != 1 or receipt.get("status") != "active":
        raise UpgradeError("unsupported automation upgrade receipt")
    required_strings = (
        "task_id",
        "branch",
        "worktree",
        "source",
        "current_version",
        "upstream_version",
        "authority_head",
        "authority_nonce",
    )
    if any(not isinstance(receipt.get(field), str) or not receipt[field] for field in required_strings):
        raise UpgradeError("automation upgrade receipt has invalid provenance fields")
    revision = receipt.get("source_revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise UpgradeError("automation upgrade receipt has an invalid source revision")
    if not Path(receipt["source"]).is_absolute():
        raise UpgradeError("automation upgrade receipt source must be absolute")
    if not re.fullmatch(r"[0-9a-f]{40,64}", receipt["authority_head"]):
        raise UpgradeError("automation upgrade receipt has an invalid authority HEAD")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt["authority_nonce"]):
        raise UpgradeError("automation upgrade receipt has an invalid authority nonce")
    return receipt


def staged_fingerprint(
    repo: Path,
    path: str,
    *,
    env_overrides: dict[str, str],
) -> dict[str, object]:
    entry = run(
        ["git", "ls-files", "--stage", "--", path],
        cwd=repo,
        env_overrides=env_overrides,
    ).stdout.strip()
    if not entry:
        return {"state": "absent", "mode": None, "content_sha256": None}
    fields = entry.split(maxsplit=3)
    if len(fields) != 4 or fields[2] != "0":
        raise UpgradeError(f"staged path has an unsupported index entry: {path}")
    mode = fields[0]
    result = subprocess.run(
        [str(git_executable()), "show", f":{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
        env=git_environment(env_overrides),
    )
    if result.returncode != 0:
        raise UpgradeError(f"cannot read staged content for {path}")
    if mode not in {"100644", "100755"}:
        raise UpgradeError(f"staged path has an unsupported mode: {path}")
    return {
        "state": "file",
        "mode": 0o755 if mode == "100755" else 0o644,
        "content_sha256": hashlib.sha256(result.stdout).hexdigest(),
    }


def normalized_git_fingerprint(fingerprint: object) -> dict[str, object]:
    if not isinstance(fingerprint, dict):
        raise UpgradeError("automation receipt contains an invalid path fingerprint")
    state = fingerprint.get("state")
    if state == "absent":
        return {"state": "absent", "mode": None, "content_sha256": None}
    if state != "file" or not isinstance(fingerprint.get("mode"), int):
        raise UpgradeError("automation receipt authorizes an unsupported path state")
    digest = fingerprint.get("content_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpgradeError("automation receipt contains an invalid content fingerprint")
    return {
        "state": "file",
        "mode": 0o755 if fingerprint["mode"] & 0o111 else 0o644,
        "content_sha256": digest,
    }


def commit(repo: Path, task: str, message: str) -> dict[str, str]:
    _, branch, worktree = require_registered_task(repo, task)
    active = receipt_path(repo)
    private_index = repo / ".task-state" / "automation-maintenance.index"
    require_ignored_untracked(repo, (active, consumed_receipt_path(repo), private_index))
    if not active.is_file():
        raise UpgradeError("no active successful automation upgrade receipt")
    try:
        receipt = json.loads(active.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError("invalid automation upgrade receipt") from exc
    receipt = validate_receipt_schema(receipt)
    if (receipt.get("task_id"), receipt.get("branch"), receipt.get("worktree")) != (task, branch, str(worktree)):
        raise UpgradeError("automation receipt identity does not match the current Task worktree")
    if receipt.get("authority_head") != git_head(repo):
        raise UpgradeError("automation receipt authority HEAD is stale")
    paths = receipt_paths(repo, receipt)
    fingerprints = receipt.get("path_fingerprints")
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(paths):
        raise UpgradeError("automation receipt fingerprints do not match its paths")
    if any(file_fingerprint(repo, path) != fingerprints[path] for path in paths):
        raise UpgradeError("automation receipt path content/state fingerprint changed")
    if pending_paths(repo) != paths:
        raise UpgradeError("pending paths do not exactly match the automation receipt")
    validate_authority(repo, receipt)

    consumed = consumed_receipt_path(repo)
    os.replace(active, consumed)
    consumed_receipt = dict(receipt)
    consumed_receipt["status"] = "consumed"
    consumed_receipt["commit_sha"] = None
    atomic_json_write(consumed, consumed_receipt)
    authority = authority_path(repo)
    authority.unlink(missing_ok=True)
    private_index.unlink(missing_ok=True)
    index_environment = {"GIT_INDEX_FILE": str(private_index)}
    committed = False
    commit_sha = ""
    try:
        run(["git", "read-tree", "HEAD"], cwd=repo, env_overrides=index_environment)
        run(["git", "add", "--", *paths], cwd=repo, env_overrides=index_environment)
        run(
            ["git", "diff", "--no-ext-diff", "--cached", "--check"],
            cwd=repo,
            env_overrides=index_environment,
        )
        staged = sorted(
            run(
                ["git", "diff", "--no-ext-diff", "--cached", "--name-only"],
                cwd=repo,
                env_overrides=index_environment,
            ).stdout.splitlines()
        )
        if staged != paths:
            raise UpgradeError("staged paths do not exactly match the automation receipt")
        if any(file_fingerprint(repo, path) != fingerprints[path] for path in paths):
            raise UpgradeError("automation receipt path changed while staging")
        for path in paths:
            if staged_fingerprint(
                repo,
                path,
                env_overrides=index_environment,
            ) != normalized_git_fingerprint(fingerprints[path]):
                raise UpgradeError(f"staged content does not match the automation receipt: {path}")
        commit_message = message.strip() or f"task: {task}"
        if task not in commit_message:
            commit_message = f"{commit_message}\n\nTask: {task}"
        tree = run(
            ["git", "write-tree"],
            cwd=repo,
            env_overrides=index_environment,
        ).stdout.strip()
        expected_head = receipt["authority_head"]
        commit_sha = run(
            ["git", "commit-tree", tree, "-p", expected_head],
            cwd=repo,
            input_text=commit_message + "\n",
        ).stdout.strip()
        run(
            ["git", "update-ref", f"refs/heads/{branch}", commit_sha, expected_head],
            cwd=repo,
        )
        committed = True
        consumed_receipt["commit_sha"] = commit_sha
        atomic_json_write(consumed, consumed_receipt)
        run(["git", "reset", "-q", commit_sha, "--", *paths], cwd=repo)
    except UpgradeError:
        if not committed and not active.exists():
            atomic_json_write(active, receipt)
            write_authority(repo, receipt)
            consumed.unlink(missing_ok=True)
        raise
    finally:
        private_index.unlink(missing_ok=True)
    return {"status": "COMMITTED", "commit_sha": commit_sha}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agent Core version/update/upgrade contract")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    check = sub.add_parser("check-update")
    check.add_argument("--source", type=Path, required=True)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--source", type=Path, required=True)
    bootstrap = sub.add_parser("bootstrap-receipt")
    bootstrap.add_argument("--source", type=Path, required=True)
    commit_parser = sub.add_parser("commit")
    commit_parser.add_argument("task")
    commit_parser.add_argument("message", nargs="?", default="")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        repo = root()
        if args.command == "version":
            result = context(repo)
        elif args.command == "check-update":
            result = check_update(repo, args.source)
        elif args.command == "upgrade":
            result = apply(repo, args.source)
        elif args.command == "bootstrap-receipt":
            result = bootstrap_receipt(repo, args.source)
        elif args.command == "commit":
            result = commit(repo, args.task, args.message)
        else:  # pragma: no cover
            raise UpgradeError(f"unsupported command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except UpgradeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
