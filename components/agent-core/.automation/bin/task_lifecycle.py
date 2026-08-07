#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_STATES = {
    "initialized",
    "researching",
    "planning",
    "implementing",
    "verification-pending",
    "local-verified",
    "review-pending",
    "publication-ready",
    "draft-pr-created",
    "integration-pending",
    "merged",
    "blocked",
    "cancelled",
}
TERMINAL_STATES = {"merged", "cancelled"}


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeRecord:
    path: Path
    branch: str | None
    head: str | None


def run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise LifecycleError(f"{' '.join(command)}: {detail}")
    return result


def git(*args: str, cwd: Path, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def gh(*args: str, cwd: Path, check: bool = True) -> str:
    return run(["gh", *args], cwd=cwd, check=check).stdout.strip()


def repo_root(cwd: Path | None = None) -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip()).resolve()


def common_git_dir(root: Path) -> Path:
    value = Path(git("rev-parse", "--git-common-dir", cwd=root))
    return value if value.is_absolute() else (root / value).resolve()


def default_branch(root: Path) -> str:
    symbolic = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=root, check=False)
    if symbolic.startswith("origin/"):
        return symbolic.removeprefix("origin/")
    result = run(["gh", "repo", "view", "--json", "defaultBranchRef"], cwd=root, check=False)
    if result.returncode == 0:
        try:
            name = json.loads(result.stdout).get("defaultBranchRef", {}).get("name")
        except json.JSONDecodeError:
            name = None
        if name:
            return name
    raise LifecycleError("cannot resolve default branch; configure origin/HEAD or GitHub CLI access")


def validate_task(task: str) -> None:
    if not TASK_RE.fullmatch(task):
        raise LifecycleError(f"invalid Task ID: {task!r}")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.fullmatch(slug):
        raise LifecycleError(f"invalid Task slug: {slug!r}")


def parse_worktrees(root: Path) -> list[WorktreeRecord]:
    records: list[WorktreeRecord] = []
    current: dict[str, str] = {}
    for line in git("worktree", "list", "--porcelain", cwd=root).splitlines() + [""]:
        if not line:
            if current:
                branch = current.get("branch")
                records.append(
                    WorktreeRecord(
                        path=Path(current["worktree"]).resolve(),
                        branch=branch.removeprefix("refs/heads/") if branch else None,
                        head=current.get("HEAD"),
                    )
                )
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def worktree_for_task(root: Path, task: str) -> WorktreeRecord:
    validate_task(task)
    candidates = [
        record
        for record in parse_worktrees(root)
        if record.branch and (record.branch.startswith("task/") or record.branch.startswith("fix/")) and task in record.branch
    ]
    if len(candidates) != 1:
        raise LifecycleError(f"expected exactly one registered worktree for {task}, found {len(candidates)}")
    return candidates[0]


def ensure_excludes(root: Path) -> None:
    exclude = common_git_dir(root) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    required = ["/.task-state/"]
    missing = [line for line in required if line not in existing]
    if missing:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            for line in missing:
                handle.write(line + "\n")


def state_path(worktree: Path) -> Path:
    return worktree / ".task-state" / "task.md"


def state_status(path: Path) -> str:
    if not path.is_file():
        raise LifecycleError(f"missing Task State: {path}")
    match = re.search(r"(?m)^- Status: ([A-Za-z0-9._-]+)$", path.read_text(encoding="utf-8"))
    if not match or match.group(1) not in VALID_STATES:
        raise LifecycleError(f"invalid or missing Task State status in {path}")
    return match.group(1)


def set_state_status(path: Path, status: str) -> None:
    if status not in VALID_STATES:
        raise LifecycleError(f"invalid Task State status: {status}")
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^- Status: [A-Za-z0-9._-]+$", f"- Status: {status}", text, count=1)
    if count != 1:
        raise LifecycleError(f"cannot update Task State status in {path}")
    path.write_text(updated, encoding="utf-8")


def initialize_state(worktree: Path, task: str, branch: str, base: str, base_revision: str) -> None:
    template = worktree / ".automation" / "templates" / "task-state.md"
    if not template.is_file():
        raise LifecycleError(f"missing Task State template: {template}")
    destination = state_path(worktree)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8")
    values = {
        "@@TASK_ID@@": task,
        "@@BRANCH@@": branch,
        "@@WORKTREE@@": str(worktree),
        "@@BASE_BRANCH@@": base,
        "@@BASE_REVISION@@": base_revision,
    }
    for marker, value in values.items():
        text = text.replace(marker, value)
    destination.write_text(text, encoding="utf-8")


def assert_task_identity(record: WorktreeRecord, task: str) -> None:
    if not record.branch or task not in record.branch:
        raise LifecycleError(f"worktree branch does not match Task {task}: {record.branch}")
    expected_prefixes = ("task/", "fix/")
    if not record.branch.startswith(expected_prefixes):
        raise LifecycleError(f"invalid Task branch: {record.branch}")
    state = state_path(record.path)
    if not state.is_file():
        raise LifecycleError(f"missing Task State for {task}: {state}")
    text = state.read_text(encoding="utf-8")
    expected = {
        f"- Task ID: {task}",
        f"- Branch: {record.branch}",
        f"- Worktree: {record.path}",
    }
    missing = [line for line in expected if line not in text]
    if missing:
        raise LifecycleError("Task State identity mismatch: " + ", ".join(missing))


def task_start(root: Path, task: str, slug: str) -> None:
    validate_task(task)
    validate_slug(slug)
    branch = f"task/{task}-{slug}"
    worktree = root / ".worktrees" / f"{task}-{slug}"
    records = parse_worktrees(root)
    if any(record.branch == branch for record in records):
        raise LifecycleError(f"branch is already registered in a worktree: {branch}")
    if any(record.path == worktree.resolve() for record in records):
        raise LifecycleError(f"worktree is already registered: {worktree}")
    if worktree.exists():
        raise LifecycleError(f"worktree path already exists: {worktree}")
    if run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False).returncode == 0:
        raise LifecycleError(f"branch already exists: {branch}")

    base = default_branch(root)
    remote_base = f"refs/remotes/origin/{base}"
    base_revision = git("rev-parse", "--verify", remote_base, cwd=root, check=False)
    if not base_revision:
        base_revision = git("rev-parse", "--verify", base, cwd=root)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "-b", branch, str(worktree), base_revision], cwd=root)
    try:
        ensure_excludes(worktree)
        initialize_state(worktree, task, branch, base, base_revision)
    except Exception:
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=False)
        run(["git", "branch", "-D", branch], cwd=root, check=False)
        raise
    print(json.dumps({"task": task, "branch": branch, "worktree": str(worktree), "base": base, "baseRevision": base_revision, "status": "initialized"}))


def task_status(root: Path, task: str) -> None:
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    status = state_status(state_path(record.path))
    dirty = git("status", "--short", cwd=record.path).splitlines()
    print(json.dumps({"task": task, "branch": record.branch, "worktree": str(record.path), "head": record.head, "status": status, "dirty": dirty}))


def task_state_set(root: Path, task: str, status: str) -> None:
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    set_state_status(state_path(record.path), status)
    print(json.dumps({"task": task, "status": status}))


def task_cleanup(root: Path, task: str) -> None:
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    status = state_status(state_path(record.path))
    if status not in TERMINAL_STATES:
        raise LifecycleError(f"cleanup refused while Task status is {status}; expected one of {sorted(TERMINAL_STATES)}")
    if git("status", "--porcelain", cwd=record.path):
        raise LifecycleError("cleanup refused: Task worktree has uncommitted changes")
    branch = record.branch
    assert branch is not None
    pr_result = run(["gh", "pr", "view", branch, "--json", "state,headRefName"], cwd=record.path, check=False)
    if status == "merged":
        if pr_result.returncode != 0:
            raise LifecycleError("cleanup refused: merged Task has no resolvable pull request")
        data = json.loads(pr_result.stdout)
        if data.get("state") != "MERGED" or data.get("headRefName") != branch:
            raise LifecycleError("cleanup refused: Task pull request is not merged for the expected branch")
    elif pr_result.returncode == 0:
        data = json.loads(pr_result.stdout)
        if data.get("state") == "OPEN":
            raise LifecycleError("cleanup refused: cancelled Task still has an open pull request")

    run(["git", "worktree", "remove", str(record.path)], cwd=root)
    run(["git", "branch", "-d", branch], cwd=root)
    print(json.dumps({"task": task, "removedWorktree": str(record.path), "removedBranch": branch}))


def extract_list(path: Path, heading: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    pattern = rf"(?ms)^## {re.escape(heading)}\n\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text)
    if not match:
        return []
    values: list[str] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            value = line[2:].strip()
            if value and value.lower() not in {"none", "none recorded", "tbd"}:
                values.append(value)
    return values


def task_summary(root: Path, task: str) -> dict:
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    path = state_path(record.path)
    return {
        "task": task,
        "branch": record.branch,
        "worktree": str(record.path),
        "status": state_status(path),
        "dependencies": extract_list(path, "Dependencies"),
        "scope": extract_list(path, "Scope"),
    }


def batch_plan(root: Path, tasks: list[str]) -> None:
    if len(tasks) < 2:
        raise LifecycleError("batch-plan requires at least two explicit Task IDs")
    if len(set(tasks)) != len(tasks):
        raise LifecycleError("batch-plan contains duplicate Task IDs")
    summaries = [task_summary(root, task) for task in tasks]
    conflicts: list[dict] = []
    shared_hotspots = {"flake.nix", "flake.lock", "package-lock.json", "pnpm-lock.yaml", "Cargo.lock", "pyproject.toml", "schema", "migration", "workflow", "opencode.json", "Justfile"}

    for index, left in enumerate(summaries):
        for right in summaries[index + 1 :]:
            reasons: list[str] = []
            left_deps = set(left["dependencies"])
            right_deps = set(right["dependencies"])
            if right["task"] in left_deps or left["task"] in right_deps:
                reasons.append("declared dependency")
            left_scope = {item.lower() for item in left["scope"]}
            right_scope = {item.lower() for item in right["scope"]}
            overlap = sorted(left_scope & right_scope)
            if overlap:
                reasons.append("overlapping declared scope: " + ", ".join(overlap))
            combined = " ".join(left_scope | right_scope)
            hotspots = sorted(item for item in shared_hotspots if item.lower() in combined)
            if hotspots:
                reasons.append("shared coordination hotspot: " + ", ".join(hotspots))
            if reasons:
                conflicts.append({"tasks": [left["task"], right["task"]], "reasons": reasons})

    print(json.dumps({"tasks": summaries, "parallelSafe": not conflicts, "conflicts": conflicts}))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Task/worktree lifecycle")
    sub = result.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("task")
    start.add_argument("slug")
    status = sub.add_parser("status")
    status.add_argument("task")
    state = sub.add_parser("state-set")
    state.add_argument("task")
    state.add_argument("status")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("task")
    batch = sub.add_parser("batch-plan")
    batch.add_argument("tasks", nargs="+")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        root = repo_root()
        if args.command == "start":
            task_start(root, args.task, args.slug)
        elif args.command == "status":
            task_status(root, args.task)
        elif args.command == "state-set":
            task_state_set(root, args.task, args.status)
        elif args.command == "cleanup":
            task_cleanup(root, args.task)
        elif args.command == "batch-plan":
            batch_plan(root, args.tasks)
        return 0
    except (LifecycleError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
