#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
WORK_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RECOVERY_OUTCOMES = {"completed", "blocked", "needs-approval", "needs-decision", "failed"}
WORK_UNIT_ROLES = {"general", "explore", "verifier", "reviewer", "investigator", "security-reviewer", "scout"}
WORK_UNIT_STATES = {"in-flight", "failed", "completed", "blocked", "needs-approval", "needs-decision"}
RECOVERABLE_WORK_UNIT_STATES = {"in-flight", "failed"}
WORK_UNIT_TRANSITIONS = {
    "in-flight": {"failed", "completed", "blocked", "needs-approval", "needs-decision"},
    "failed": set(),
    "completed": set(),
    "blocked": set(),
    "needs-approval": set(),
    "needs-decision": set(),
}
RECOVERY_WORK_UNIT_TRANSITIONS = {
    "in-flight": RECOVERY_OUTCOMES,
    "failed": RECOVERY_OUTCOMES,
}
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
LINEAR_TRANSITIONS = {
    "initialized": {"researching", "planning", "blocked", "cancelled"},
    "researching": {"planning", "blocked", "cancelled"},
    "planning": {"implementing", "blocked", "cancelled"},
    "implementing": {"verification-pending", "blocked", "cancelled"},
    "verification-pending": {"implementing", "local-verified", "blocked", "cancelled"},
    "local-verified": {"review-pending", "implementing", "blocked", "cancelled"},
    "review-pending": {"publication-ready", "implementing", "blocked", "cancelled"},
    "publication-ready": {"draft-pr-created", "implementing", "blocked", "cancelled"},
    "draft-pr-created": {"integration-pending", "implementing", "blocked", "cancelled"},
    "integration-pending": {"merged", "implementing", "blocked", "cancelled"},
    "blocked": {"planning", "implementing", "verification-pending", "cancelled"},
    "merged": set(),
    "cancelled": set(),
}


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeRecord:
    path: Path
    branch: str | None
    head: str | None


def run(
    command: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise LifecycleError(f"{' '.join(command)}: {detail}")
    return result


def git(*args: str, cwd: Path, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def repo_root(cwd: Path | None = None) -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip()).resolve()


def common_git_dir(root: Path) -> Path:
    value = Path(git("rev-parse", "--git-common-dir", cwd=root))
    return value if value.is_absolute() else (root / value).resolve()


def default_branch(root: Path) -> str:
    symbolic = git(
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        cwd=root,
        check=False,
    )
    if symbolic.startswith("origin/"):
        return symbolic.removeprefix("origin/")
    result = run(
        ["gh", "repo", "view", "--json", "defaultBranchRef"],
        cwd=root,
        check=False,
    )
    if result.returncode == 0:
        try:
            name = json.loads(result.stdout).get("defaultBranchRef", {}).get("name")
        except json.JSONDecodeError:
            name = None
        if name:
            return name
    raise LifecycleError(
        "cannot resolve default branch; configure origin/HEAD or GitHub CLI access"
    )


def validate_task(task: str) -> None:
    if not TASK_RE.fullmatch(task):
        raise LifecycleError(f"invalid Task ID: {task!r}")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.fullmatch(slug):
        raise LifecycleError(f"invalid Task slug: {slug!r}")


def branch_matches_task(branch: str | None, task: str) -> bool:
    if not branch:
        return False
    return branch.startswith(f"task/{task}-") or branch.startswith(f"fix/{task}-")


def parse_worktrees(root: Path) -> list[WorktreeRecord]:
    records: list[WorktreeRecord] = []
    current: dict[str, str] = {}
    lines = git("worktree", "list", "--porcelain", cwd=root).splitlines() + [""]
    for line in lines:
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


def current_worktree(root: Path) -> WorktreeRecord:
    matches = [record for record in parse_worktrees(root) if record.path == root.resolve()]
    if len(matches) != 1:
        raise LifecycleError(
            f"cannot uniquely resolve current worktree {root}: found {len(matches)}"
        )
    return matches[0]


def main_worktree(root: Path) -> WorktreeRecord:
    base = default_branch(root)
    matches = [record for record in parse_worktrees(root) if record.branch == base]
    if len(matches) != 1:
        raise LifecycleError(
            f"cannot uniquely resolve default-branch worktree for {base}: found {len(matches)}"
        )
    return matches[0]


def require_main_worktree(root: Path) -> WorktreeRecord:
    current = current_worktree(root)
    main = main_worktree(root)
    if current.path != main.path or current.branch != main.branch:
        raise LifecycleError(
            f"operation must run from the default-branch worktree: {main.path}"
        )
    return current


def worktree_for_task(root: Path, task: str) -> WorktreeRecord:
    validate_task(task)
    candidates = [
        record for record in parse_worktrees(root) if branch_matches_task(record.branch, task)
    ]
    if len(candidates) != 1:
        raise LifecycleError(
            f"expected exactly one registered worktree for {task}, found {len(candidates)}"
        )
    return candidates[0]


def require_local_task(root: Path, task: str) -> WorktreeRecord:
    record = worktree_for_task(root, task)
    if record.path != root.resolve():
        raise LifecycleError(
            f"Task {task} belongs to sibling worktree {record.path}; current worktree is {root}"
        )
    assert_task_identity(record, task)
    return record


def ensure_excludes(root: Path) -> None:
    exclude = common_git_dir(root) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if "/.task-state/" not in existing:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            handle.write("/.task-state/\n")


def state_path(worktree: Path) -> Path:
    return worktree / ".task-state" / "task.md"


def recovery_path(worktree: Path) -> Path:
    return worktree / ".task-state" / "recovery.json"


def work_units_path(worktree: Path) -> Path:
    return worktree / ".task-state" / "work-units.json"


def fallback_module(root: Path):
    path = root / ".automation" / "bin" / "model_fallback.py"
    if not path.is_file():
        raise LifecycleError(f"missing fallback policy helper: {path}")
    spec = importlib.util.spec_from_file_location("model_fallback_recovery", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def append_recovery_evidence(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8")
    heading = "### Model fallback recovery"
    if heading in text:
        text = text.replace(heading, heading + "\n- " + line, 1)
    elif "## Evidence" in text:
        text = text.replace("## Evidence", "## Evidence\n\n" + heading + "\n- " + line, 1)
    else:
        text += "\n## Evidence\n\n" + heading + "\n- " + line
    path.write_text(text, encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def semantic_digest(objective: str) -> str:
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def validate_objective(objective: str) -> None:
    if not objective or len(objective) > 2000 or any(ord(char) < 32 for char in objective):
        raise LifecycleError("Work Unit objective must be a non-empty single line of at most 2000 characters")


def empty_work_units(record: WorktreeRecord, task: str) -> dict:
    return {
        "schema_version": 1,
        "task_id": task,
        "worktree": str(record.path),
        "branch": record.branch,
        "units": {},
    }


def read_work_units(record: WorktreeRecord, task: str) -> dict:
    path = work_units_path(record.path)
    if not path.is_file():
        return empty_work_units(record, task)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"invalid Work Unit state JSON: {path}") from exc
    expected = {"schema_version": 1, "task_id": task, "worktree": str(record.path), "branch": record.branch}
    mismatches = [key for key, expected_value in expected.items() if value.get(key) != expected_value]
    if mismatches or not isinstance(value.get("units"), dict):
        raise LifecycleError("Work Unit state identity mismatch: " + ", ".join(mismatches or ["units"]))
    return value


def work_unit_register(root: Path, task: str, work_unit: str, role: str, objective: str) -> None:
    record = require_local_task(root, task)
    if not WORK_UNIT_RE.fullmatch(work_unit):
        raise LifecycleError(f"invalid Work Unit ID: {work_unit!r}")
    if role not in WORK_UNIT_ROLES:
        raise LifecycleError(f"invalid Work Unit role: {role}")
    validate_objective(objective)
    value = read_work_units(record, task)
    if work_unit in value["units"]:
        raise LifecycleError(f"Work Unit already exists: {work_unit}")
    now = utc_now()
    unit = {
        "id": work_unit,
        "requested_role": role,
        "objective": objective,
        "semantic_sha256": semantic_digest(objective),
        "state": "in-flight",
        "created_at": now,
        "updated_at": now,
    }
    value["units"][work_unit] = unit
    atomic_json(work_units_path(record.path), value)
    append_recovery_evidence(
        state_path(record.path),
        f"work_unit_registered={work_unit}; requested_role={role}; semantic_sha256={unit['semantic_sha256']}; state=in-flight",
    )
    print(json.dumps(unit, sort_keys=True))


def work_unit_status(root: Path, task: str, work_unit: str) -> None:
    record = require_local_task(root, task)
    unit = read_work_units(record, task)["units"].get(work_unit)
    if unit is None:
        raise LifecycleError(f"unknown Work Unit: {work_unit}")
    print(json.dumps(unit, sort_keys=True))


def work_unit_state_set(root: Path, task: str, work_unit: str, status: str) -> None:
    record = require_local_task(root, task)
    if status not in WORK_UNIT_STATES:
        raise LifecycleError(f"invalid Work Unit state: {status}")
    value = read_work_units(record, task)
    unit = value["units"].get(work_unit)
    if unit is None:
        raise LifecycleError(f"unknown Work Unit: {work_unit}")
    previous = unit.get("state")
    if status == previous:
        print(json.dumps(unit, sort_keys=True))
        return
    if status not in WORK_UNIT_TRANSITIONS.get(previous, set()):
        raise LifecycleError(f"invalid Work Unit transition: {previous} -> {status}")
    unit["state"] = status
    unit["updated_at"] = utc_now()
    atomic_json(work_units_path(record.path), value)
    print(json.dumps(unit, sort_keys=True))


def validate_policy_agent_bindings(helper, cfg: dict, task_root: Path, caller_root: Path) -> None:
    roots = [task_root]
    if caller_root.resolve() != task_root.resolve():
        roots.append(caller_root)
    try:
        helper.validate_project_permission_binding(roots)
        for role in cfg.get("roles", {}):
            helper.validate_agent_binding(role, cfg, roots)
    except helper.FallbackError as exc:
        raise LifecycleError(str(exc)) from exc


def validate_recovery_identity(value: dict, record: WorktreeRecord, task: str) -> None:
    executable_root = main_worktree(record.path).path
    expected = {
        "task_id": task,
        "worktree": str(record.path),
        "branch": record.branch,
        "active": True,
        "reason": "usage_limit",
        "source": "operator",
        "executable_root": str(executable_root),
    }
    mismatches = [key for key, expected_value in expected.items() if value.get(key) != expected_value]
    if mismatches:
        raise LifecycleError("recovery state identity mismatch: " + ", ".join(mismatches))


def recovery_start(root: Path, task: str, family: str) -> None:
    require_main_worktree(root)
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    status = state_status(state_path(record.path))
    if status in TERMINAL_STATES:
        raise LifecycleError(f"recovery requires nonterminal Task State, found {status}")
    # Execute only the guarded caller worktree's helper code. The target Task's
    # policy data is authoritative, but must not become executable Main code.
    helper = fallback_module(root)
    cfg = helper.policy(record.path)
    if family not in cfg.get("families", {}):
        raise LifecycleError(f"unknown model family: {family}")
    validate_policy_agent_bindings(helper, cfg, record.path, root)
    path = recovery_path(record.path)
    if path.exists():
        raise LifecycleError(f"recovery state already exists for {task}; clear it explicitly first")
    try:
        routes = {role: helper.recovery_route(role, family, cfg) for role in cfg.get("roles", {})}
    except helper.FallbackError as exc:
        raise LifecycleError(str(exc)) from exc
    orchestrator = routes.get("task-orchestrator")
    if not orchestrator or orchestrator.get("status") == "BLOCKED":
        raise LifecycleError("recovery Task Orchestrator route is BLOCKED")
    now = utc_now()
    value = {
        "schema_version": 1,
        "active": True,
        "reason": "usage_limit",
        "source": "operator",
        "executable_root": str(root.resolve()),
        "task_id": task,
        "worktree": str(record.path),
        "branch": record.branch,
        "started_at": now,
        "updated_at": now,
        "unavailable_family": family,
        "routing": {role: route.get("agent") for role, route in routes.items() if route.get("agent")},
        "routes": routes,
        "events": [],
        "recoverable_work_units": [
            unit for unit in read_work_units(record, task)["units"].values()
            if unit.get("state") in RECOVERABLE_WORK_UNIT_STATES
        ],
    }
    atomic_json(path, value)
    append_recovery_evidence(
        state_path(record.path),
        f"started task={task}; reason=operator-asserted usage-limit observation (runtime unverified); "
        f"unavailable_family={family}; "
        f"task_orchestrator={orchestrator['agent']}; model={orchestrator['model']}; routing="
        + ",".join(f"{role}->{agent}" for role, agent in sorted(value["routing"].items())),
    )
    print(json.dumps(value, sort_keys=True))


def recovery_read(root: Path, task: str) -> tuple[WorktreeRecord, dict]:
    current, main = current_worktree(root), main_worktree(root)
    record = worktree_for_task(root, task)
    if current.path not in {main.path, record.path}:
        raise LifecycleError(f"cannot inspect sibling Task worktree {record.path} from {current.path}")
    assert_task_identity(record, task)
    path = recovery_path(record.path)
    if not path.is_file():
        raise LifecycleError(f"no active recovery state for {task}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"invalid recovery state JSON: {path}") from exc
    validate_recovery_identity(value, record, task)
    return record, value


def recovery_status(root: Path, task: str) -> None:
    _, value = recovery_read(root, task)
    if not value.get("active"):
        raise LifecycleError(f"no active recovery state for {task}")
    print(json.dumps(value, sort_keys=True))


def recovery_route(root: Path, task: str, role: str) -> None:
    record, value = recovery_read(root, task)
    if not value.get("active"):
        raise LifecycleError(f"no active recovery state for {task}")
    executable_root = Path(value["executable_root"]).resolve()
    helper = fallback_module(executable_root)
    cfg = helper.policy(record.path)
    validate_policy_agent_bindings(
        helper, cfg, record.path, executable_root
    )
    try:
        route = helper.recovery_route(role, value["unavailable_family"], cfg)
    except helper.FallbackError as exc:
        raise LifecycleError(str(exc)) from exc
    print(json.dumps({"role": role, "primary": route["agents"][0], "primary_model": route["models"][0], "selected": route.get("agent"),
                      "model": route.get("model"), "reason": route.get("reason"),
                      "status": route.get("status")}, sort_keys=True))


def recovery_clear(root: Path, task: str) -> None:
    require_main_worktree(root)
    record, value = recovery_read(root, task)
    append_recovery_evidence(state_path(record.path), f"cleared task={task}; reason={value.get('reason')}; source={value.get('source')}")
    recovery_path(record.path).unlink()
    print(json.dumps({"task": task, "cleared": True}))


def recovery_record(root: Path, task: str, role: str, work_unit: str, semantic_sha256: str, outcome: str) -> None:
    record = require_local_task(root, task)
    if not WORK_UNIT_RE.fullmatch(work_unit):
        raise LifecycleError(f"invalid Work Unit ID: {work_unit!r}")
    if outcome not in RECOVERY_OUTCOMES:
        raise LifecycleError(f"invalid recovery outcome: {outcome}")
    work_units = read_work_units(record, task)
    unit = work_units["units"].get(work_unit)
    if unit is None:
        raise LifecycleError(f"unknown Work Unit: {work_unit}")
    if unit.get("requested_role") != role:
        raise LifecycleError(f"Work Unit role mismatch for {work_unit}")
    if unit.get("semantic_sha256") != semantic_sha256:
        raise LifecycleError(f"Work Unit semantic mismatch for {work_unit}")
    if unit.get("state") not in RECOVERABLE_WORK_UNIT_STATES:
        raise LifecycleError(f"Work Unit is not recoverable: {work_unit} ({unit.get('state')})")
    if outcome not in RECOVERY_WORK_UNIT_TRANSITIONS[unit["state"]]:
        raise LifecycleError(f"invalid recovery Work Unit transition: {unit['state']} -> {outcome}")
    path = recovery_path(record.path)
    if not path.is_file():
        raise LifecycleError(f"no active recovery state for {task}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not value.get("active"):
        raise LifecycleError(f"no active recovery state for {task}")
    validate_recovery_identity(value, record, task)
    snapshot = next(
        (candidate for candidate in value.get("recoverable_work_units", []) if candidate.get("id") == work_unit),
        None,
    )
    if snapshot is None:
        raise LifecycleError(f"Work Unit was not recoverable when recovery started: {work_unit}")
    if snapshot.get("requested_role") != role or snapshot.get("semantic_sha256") != semantic_sha256:
        raise LifecycleError(f"Work Unit recovery snapshot mismatch for {work_unit}")
    executable_root = Path(value["executable_root"]).resolve()
    helper = fallback_module(executable_root)
    cfg = helper.policy(record.path)
    validate_policy_agent_bindings(
        helper, cfg, record.path, executable_root
    )
    try:
        route = helper.recovery_route(role, value["unavailable_family"], cfg)
    except helper.FallbackError as exc:
        raise LifecycleError(str(exc)) from exc
    if route.get("status") == "BLOCKED":
        raise LifecycleError(f"no selected recovery agent for role {role}")
    event = {"requested_role": role, "work_unit": work_unit, "semantic_sha256": semantic_sha256, "selected_agent": route["agent"],
             "selected_model": route["model"], "reason": value["reason"], "outcome": outcome}
    value.setdefault("events", []).append(event)
    value["updated_at"] = utc_now()
    atomic_json(path, value)
    unit["state"] = outcome
    unit["updated_at"] = utc_now()
    atomic_json(work_units_path(record.path), work_units)
    append_recovery_evidence(state_path(record.path), "requested_role={requested_role}; selected_agent={selected_agent}; selected_model={selected_model}; reason={reason}; work_unit={work_unit}; semantic_sha256={semantic_sha256}; outcome={outcome}".format(**event))
    print(json.dumps({"recorded": True, **event}, sort_keys=True))


def state_status(path: Path) -> str:
    if not path.is_file():
        raise LifecycleError(f"missing Task State: {path}")
    match = re.search(
        r"(?m)^- Status: ([A-Za-z0-9._-]+)$", path.read_text(encoding="utf-8")
    )
    if not match or match.group(1) not in VALID_STATES:
        raise LifecycleError(f"invalid or missing Task State status in {path}")
    return match.group(1)


def set_state_status(path: Path, status: str) -> None:
    if status not in VALID_STATES:
        raise LifecycleError(f"invalid Task State status: {status}")
    previous = state_status(path)
    if status == previous:
        return
    if status not in LINEAR_TRANSITIONS[previous]:
        raise LifecycleError(f"invalid Task State transition: {previous} -> {status}")
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^- Status: [A-Za-z0-9._-]+$",
        f"- Status: {status}",
        text,
        count=1,
    )
    if count != 1:
        raise LifecycleError(f"cannot update Task State status in {path}")
    path.write_text(updated, encoding="utf-8")


def initialize_state(
    worktree: Path, task: str, branch: str, base: str, base_revision: str
) -> None:
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
    if not branch_matches_task(record.branch, task):
        raise LifecycleError(
            f"worktree branch does not match Task {task}: {record.branch}"
        )
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
    require_main_worktree(root)
    validate_task(task)
    validate_slug(slug)
    branch = f"task/{task}-{slug}"
    worktree = root / ".worktrees" / f"{task}-{slug}"
    records = parse_worktrees(root)

    if any(branch_matches_task(record.branch, task) for record in records):
        raise LifecycleError(f"Task already has a registered worktree: {task}")
    if any(record.branch == branch for record in records):
        raise LifecycleError(f"branch is already registered in a worktree: {branch}")
    if any(record.path == worktree.resolve() for record in records):
        raise LifecycleError(f"worktree is already registered: {worktree}")
    if worktree.exists():
        raise LifecycleError(f"worktree path already exists: {worktree}")
    if (
        run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root,
            check=False,
        ).returncode
        == 0
    ):
        raise LifecycleError(f"branch already exists: {branch}")

    base = default_branch(root)
    remote_base = f"refs/remotes/origin/{base}"
    base_revision = git("rev-parse", "--verify", remote_base, cwd=root, check=False)
    if not base_revision:
        base_revision = git("rev-parse", "--verify", base, cwd=root)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "worktree", "add", "-b", branch, str(worktree), base_revision],
        cwd=root,
    )
    try:
        ensure_excludes(worktree)
        initialize_state(worktree, task, branch, base, base_revision)
    except Exception:
        run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            check=False,
        )
        run(["git", "branch", "-D", branch], cwd=root, check=False)
        raise
    print(
        json.dumps(
            {
                "task": task,
                "branch": branch,
                "worktree": str(worktree),
                "base": base,
                "baseRevision": base_revision,
                "status": "initialized",
            }
        )
    )


def task_status(root: Path, task: str) -> None:
    current = current_worktree(root)
    main = main_worktree(root)
    record = worktree_for_task(root, task)
    if current.path != main.path and current.path != record.path:
        raise LifecycleError(
            f"cannot inspect sibling Task worktree {record.path} from {current.path}"
        )
    assert_task_identity(record, task)
    status = state_status(state_path(record.path))
    dirty = git("status", "--short", cwd=record.path).splitlines()
    print(
        json.dumps(
            {
                "task": task,
                "branch": record.branch,
                "worktree": str(record.path),
                "head": record.head,
                "status": status,
                "dirty": dirty,
            }
        )
    )


def task_state_set(root: Path, task: str, status: str) -> None:
    record = require_local_task(root, task)
    set_state_status(state_path(record.path), status)
    if status in TERMINAL_STATES and recovery_path(record.path).is_file():
        append_recovery_evidence(state_path(record.path), f"discarded recovery on terminal state={status}")
        recovery_path(record.path).unlink()
    print(json.dumps({"task": task, "status": status}))


def extract_identity_value(path: Path, label: str) -> str | None:
    match = re.search(
        rf"(?m)^- {re.escape(label)}: (.+)$", path.read_text(encoding="utf-8")
    )
    return match.group(1).strip() if match else None


def unpushed_commits(record: WorktreeRecord, state: Path) -> int:
    assert record.branch is not None
    upstream = git(
        "rev-parse",
        "--abbrev-ref",
        f"{record.branch}@{{upstream}}",
        cwd=record.path,
        check=False,
    )
    if upstream:
        count = git(
            "rev-list",
            "--count",
            f"{upstream}..{record.branch}",
            cwd=record.path,
        )
        return int(count or "0")

    base_revision = extract_identity_value(state, "Base revision")
    if not base_revision:
        raise LifecycleError("Task State is missing Base revision")
    count = git(
        "rev-list",
        "--count",
        f"{base_revision}..{record.branch}",
        cwd=record.path,
    )
    return int(count or "0")


def task_cleanup(root: Path, task: str) -> None:
    require_main_worktree(root)
    record = worktree_for_task(root, task)
    assert_task_identity(record, task)
    state = state_path(record.path)
    status = state_status(state)
    if status not in TERMINAL_STATES:
        raise LifecycleError(
            f"cleanup refused while Task status is {status}; expected one of {sorted(TERMINAL_STATES)}"
        )
    if git("status", "--porcelain", cwd=record.path):
        raise LifecycleError("cleanup refused: Task worktree has uncommitted changes")
    ahead = unpushed_commits(record, state)
    if ahead:
        raise LifecycleError(f"cleanup refused: Task branch has {ahead} unpushed commit(s)")

    branch = record.branch
    assert branch is not None
    pr_result = run(
        ["gh", "pr", "view", branch, "--json", "state,headRefName,headRefOid"],
        cwd=record.path,
        check=False,
    )
    if status == "merged":
        if pr_result.returncode != 0:
            raise LifecycleError("cleanup refused: merged Task has no resolvable pull request")
        data = json.loads(pr_result.stdout)
        if data.get("state") != "MERGED" or data.get("headRefName") != branch:
            raise LifecycleError(
                "cleanup refused: Task pull request is not merged for the expected branch"
            )
    elif pr_result.returncode == 0:
        data = json.loads(pr_result.stdout)
        if data.get("state") == "OPEN":
            raise LifecycleError("cleanup refused: cancelled Task still has an open pull request")

    removed_path = str(record.path)
    recovery_discarded = recovery_path(record.path).is_file()
    if recovery_discarded:
        append_recovery_evidence(state, "discarded recovery during cleanup")
    run(["git", "worktree", "remove", str(record.path)], cwd=root)
    run(["git", "branch", "-D", branch], cwd=root)
    print(
        json.dumps(
            {
                "task": task,
                "removedWorktree": removed_path,
                "removedBranch": branch,
                "taskStateDiscarded": True,
                "recoveryDiscarded": recovery_discarded,
            }
        )
    )


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
        "coordinationSurfaces": extract_list(path, "Coordination surfaces"),
        "externalResources": extract_list(path, "External resources"),
    }


def normalized(values: list[str]) -> set[str]:
    return {value.strip().lower() for value in values if value.strip()}


def overlap_reason(label: str, left: list[str], right: list[str]) -> str | None:
    overlap = sorted(normalized(left) & normalized(right))
    if not overlap:
        return None
    return f"overlapping {label}: " + ", ".join(overlap)


def batch_conflicts(summaries: list[dict]) -> list[dict]:
    conflicts: list[dict] = []
    for index, left in enumerate(summaries):
        for right in summaries[index + 1 :]:
            reasons: list[str] = []
            left_deps = normalized(left["dependencies"])
            right_deps = normalized(right["dependencies"])
            if right["task"].lower() in left_deps or left["task"].lower() in right_deps:
                reasons.append("declared dependency")
            for label, key in (
                ("declared scope", "scope"),
                ("coordination surface", "coordinationSurfaces"),
                ("external resource", "externalResources"),
            ):
                reason = overlap_reason(label, left[key], right[key])
                if reason:
                    reasons.append(reason)
            if reasons:
                conflicts.append(
                    {"tasks": [left["task"], right["task"]], "reasons": reasons}
                )
    return conflicts


def batch_plan(root: Path, tasks: list[str]) -> None:
    require_main_worktree(root)
    if len(tasks) < 2:
        raise LifecycleError("batch-plan requires at least two explicit Task IDs")
    if len(set(tasks)) != len(tasks):
        raise LifecycleError("batch-plan contains duplicate Task IDs")
    summaries = [task_summary(root, task) for task in tasks]
    conflicts = batch_conflicts(summaries)
    print(
        json.dumps(
            {
                "tasks": summaries,
                "parallelSafe": not conflicts,
                "conflicts": conflicts,
            }
        )
    )


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
    recovery_start_parser = sub.add_parser("recovery-start")
    recovery_start_parser.add_argument("task")
    recovery_start_parser.add_argument("family")
    recovery_status_parser = sub.add_parser("recovery-status")
    recovery_status_parser.add_argument("task")
    recovery_route_parser = sub.add_parser("recovery-route")
    recovery_route_parser.add_argument("task")
    recovery_route_parser.add_argument("role")
    recovery_clear_parser = sub.add_parser("recovery-clear")
    recovery_clear_parser.add_argument("task")
    recovery_record_parser = sub.add_parser("recovery-record")
    recovery_record_parser.add_argument("task")
    recovery_record_parser.add_argument("role")
    recovery_record_parser.add_argument("work_unit")
    recovery_record_parser.add_argument("semantic_sha256")
    recovery_record_parser.add_argument("outcome", choices=sorted(RECOVERY_OUTCOMES))
    work_unit_register_parser = sub.add_parser("work-unit-register")
    work_unit_register_parser.add_argument("task")
    work_unit_register_parser.add_argument("work_unit")
    work_unit_register_parser.add_argument("role")
    work_unit_register_parser.add_argument("objective")
    work_unit_status_parser = sub.add_parser("work-unit-status")
    work_unit_status_parser.add_argument("task")
    work_unit_status_parser.add_argument("work_unit")
    work_unit_state_parser = sub.add_parser("work-unit-state-set")
    work_unit_state_parser.add_argument("task")
    work_unit_state_parser.add_argument("work_unit")
    work_unit_state_parser.add_argument("status", choices=sorted(WORK_UNIT_STATES))
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
        elif args.command == "recovery-start":
            recovery_start(root, args.task, args.family)
        elif args.command == "recovery-status":
            recovery_status(root, args.task)
        elif args.command == "recovery-route":
            recovery_route(root, args.task, args.role)
        elif args.command == "recovery-clear":
            recovery_clear(root, args.task)
        elif args.command == "recovery-record":
            recovery_record(root, args.task, args.role, args.work_unit, args.semantic_sha256, args.outcome)
        elif args.command == "work-unit-register":
            work_unit_register(root, args.task, args.work_unit, args.role, args.objective)
        elif args.command == "work-unit-status":
            work_unit_status(root, args.task, args.work_unit)
        elif args.command == "work-unit-state-set":
            work_unit_state_set(root, args.task, args.work_unit, args.status)
        else:  # pragma: no cover
            raise LifecycleError(f"unsupported command: {args.command}")
    except LifecycleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
