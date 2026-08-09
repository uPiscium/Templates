#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".runtime-smoke"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

TASK_DEFINITIONS = (
    (
        "SMOKE-CONTROL",
        "ask-free-control",
        "Diagnose whether a nested Depth-2 leaf can complete without any Ask permission event.",
        [
            "Task Orchestrator must delegate exactly one Work Unit to `general`.",
            "The leaf must run only `git status --short` once and return the result.",
            "The leaf must not edit files and must not request any unclassified Bash command.",
        ],
        [
            "Task Orchestrator launches the Depth-2 leaf.",
            "A provider response is observed for the leaf.",
            "`git status --short` completes without a permission dialog.",
            "The leaf returns to its parent instead of remaining busy indefinitely.",
        ],
        ["Run from Main TUI with `/task-run SMOKE-CONTROL` under `just runtime::diagnose-child-stall`."],
    ),
    (
        "SMOKE-ASK",
        "depth2-ask",
        "Exercise a harmless Depth-2 Bash Ask from a leaf and observe approval/rejection relay in Main TUI.",
        [
            "Task Orchestrator must delegate the Ask probe to exactly one `general` leaf.",
            "The leaf first requests `printf 'depth2-ask-approved\\n'` through Bash and waits for approval.",
            "After the first probe resolves, the leaf requests `printf 'depth2-ask-rejected\\n'` and waits for rejection.",
            "Do not perform unrelated implementation or repository changes.",
        ],
        [
            "Depth-2 Ask is visible from the Main TUI.",
            "Single-command approval propagates to the leaf.",
            "Rejection propagates to the leaf without weakening permissions.",
        ],
        ["Run from Main TUI with `/task-run SMOKE-ASK` under `just runtime::smoke-depth2`."],
    ),
    (
        "SMOKE-FALLBACK",
        "model-fallback",
        "Observe genuine usage/quota/rate-limit fallback behavior without manufacturing a provider failure.",
        [
            "Task Orchestrator may delegate one trivial read-only Work Unit to a leaf.",
            "Do not intentionally consume quota or damage credentials/model configuration.",
            "If no genuine usage-limit condition occurs, record runtime fallback as INCOMPLETE.",
        ],
        [
            "Any genuine eligible failure is classified before fallback.",
            "Only the configured fallback variant is selected.",
            "No genuine trigger is reported as INCOMPLETE rather than PASS.",
        ],
        ["Run from Main TUI with `/task-run SMOKE-FALLBACK` under `just runtime::smoke-fallback`."],
    ),
)


class RuntimeSmokeError(RuntimeError):
    pass


def validate_name(value: str, label: str) -> str:
    if not SAFE_NAME.fullmatch(value):
        raise RuntimeSmokeError(f"invalid {label}: {value!r}")
    return value


def workspace(issue: str) -> Path:
    return RUNTIME_ROOT / validate_name(issue, "issue name")


def smoke_repo(issue: str) -> Path:
    return workspace(issue) / "smoke-repo"


def run_capture(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeSmokeError(f"{' '.join(command)}: {detail}")
    return result.stdout.strip()


def tail(path: Path, count: int = 80) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:])


def run_logged(command: list[str], *, cwd: Path, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(command)}\n")
        handle.flush()
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise RuntimeSmokeError(
            f"{' '.join(command)} failed with exit {result.returncode}\n{tail(log)}"
        )


def required_tools() -> list[str]:
    return ["git", "nix", "just", "python3", "opencode"]


def doctor() -> dict:
    missing = [tool for tool in required_tools() if shutil.which(tool) is None]
    if missing:
        raise RuntimeSmokeError("missing runtime tools: " + ", ".join(missing))
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    if "/.runtime-smoke/" not in ignore:
        raise RuntimeSmokeError("/.runtime-smoke/ is not ignored by the Templates repository")
    return {
        "status": "PASS",
        "repositoryRoot": str(ROOT),
        "runtimeRoot": str(RUNTIME_ROOT),
        "opencodeVersion": run_capture(["opencode", "--version"], cwd=ROOT),
    }


def identity(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for label, key in (
        ("Task ID", "task"),
        ("Branch", "branch"),
        ("Worktree", "worktree"),
        ("Base branch", "base"),
        ("Base revision", "base_revision"),
    ):
        match = re.search(rf"(?m)^- {re.escape(label)}: (.+)$", text)
        if not match:
            raise RuntimeSmokeError(f"Task State missing {label}: {path}")
        result[key] = match.group(1).strip()
    return result


def write_contract(
    path: Path,
    *,
    purpose: str,
    scope: list[str],
    acceptance: list[str],
    test_plan: list[str],
) -> None:
    item = identity(path)
    scope_text = "\n".join(f"- {entry}" for entry in scope)
    acceptance_text = "\n".join(f"- [ ] {entry}" for entry in acceptance)
    test_text = "\n".join(f"- {entry}" for entry in test_plan)
    text = f"""# {item['task']}

## Identity

- Task ID: {item['task']}
- Branch: {item['branch']}
- Worktree: {item['worktree']}
- Base branch: {item['base']}
- Base revision: {item['base_revision']}

## Purpose

{purpose}

## Scope

{scope_text}

## Coordination surfaces

- OpenCode Main TUI
- Task Orchestrator child session
- Depth-2 leaf session when required by this Task

## External resources

- Local `.runtime-smoke/` evidence only

## Prohibited changes

- Do not modify tracked repository files.
- Do not modify Agent Core, Project Adapter, permissions, credentials, or configured model IDs.
- Do not commit, push, create a PR, or merge.
- Do not weaken an Ask/deny boundary to make the diagnostic pass.

## Dependencies

- Main runtime initialization passed.
- Task worktree identity and project environment checks passed.

## Acceptance criteria

{acceptance_text}

## Test plan

{test_text}

## Stop conditions

- A permission or configured model must be changed to continue.
- Provider credentials would need to be modified.
- The requested diagnostic leaves the assigned worktree.

## Current state

- Status: initialized
- Blockers: none
- Unverified: runtime observations

## Work Units

None yet.

## Evidence

### Changed files

None expected.

### Commands

None recorded yet.

### Reviews

None.

### Git

- Commit: none
- Remote branch: none
- Pull request: none
- Published head SHA: none

## Follow-up Task candidates

None yet.
"""
    path.write_text(text, encoding="utf-8")


def report_template(issue: str, metadata: dict) -> str:
    return f"""# Runtime Diagnostic Report — {issue}

## Environment

- Templates commit: `{metadata['templatesCommit']}`
- OpenCode version: `{metadata['opencodeVersion']}`
- Template: `{metadata['template']}`
- Fixture: `{metadata['smokeRepo']}`
- Created: {metadata['createdAt']}

## Preparation

- `runtime::doctor`: PASS
- fixture bootstrap: PASS
- Main initialization: PASS
- SMOKE-CONTROL contract: READY
- SMOKE-ASK contract: READY
- SMOKE-FALLBACK contract: READY

## Ask-free nested control

- Log:
- Main session ID:
- Task Orchestrator session ID:
- Leaf session ID:
- child session created: PASS / FAIL / INCOMPLETE
- provider request started: PASS / FAIL / INCOMPLETE
- provider response observed: PASS / FAIL / INCOMPLETE
- read-only tool completed: PASS / FAIL / INCOMPLETE
- child returned to parent: PASS / FAIL / INCOMPLETE
- result: PASS / FAIL / INCOMPLETE

## Direct leaf control

- Events log:
- Debug log:
- `general` provider response: PASS / FAIL / INCOMPLETE
- `git status --short` completed: PASS / FAIL / INCOMPLETE
- result: PASS / FAIL / INCOMPLETE

## Depth-2 Ask probe

- Log:
- Main session ID:
- Task Orchestrator session ID:
- Leaf session ID:
- provider response before Ask: PASS / FAIL / INCOMPLETE
- `permission.asked` observed: PASS / FAIL / INCOMPLETE
- Ask visible in Main TUI: PASS / FAIL / INCOMPLETE
- approval propagation: PASS / FAIL / INCOMPLETE
- rejection propagation: PASS / FAIL / INCOMPLETE
- result: PASS / FAIL / INCOMPLETE

## Model fallback observation

- deterministic preflight: PASS / FAIL / INCOMPLETE
- genuine usage-limit observed: YES / NO
- runtime fallback: PASS / FAIL / INCOMPLETE

## Diagnosis

- suspected stop stage:
- provider/session stall vs permission relay:
- upstream issue/version evidence:
- repository-local config evidence:

## Repository integrity

- Templates checkout dirty: YES / NO
- fixture tracked changes: YES / NO
- commits/push/PR/merge beyond fixture setup: YES / NO

## Final verdict

- #41 diagnostic acceptance: PASS / FAIL / INCOMPLETE
- #7 ready for full permission smoke: YES / NO
- #23 runtime acceptance: PASS / FAIL / INCOMPLETE

## Notes

Record only evidence actually observed. Do not infer PASS from static checks.
"""


def prepare(issue: str, template: str) -> dict:
    issue = validate_name(issue, "issue name")
    template = validate_name(template, "template")
    doctor()
    base = workspace(issue)
    if base.exists():
        raise RuntimeSmokeError(
            f"runtime workspace already exists: {base}; preserve it as evidence or remove it explicitly before a fresh run"
        )

    logs = base / "logs"
    reports = base / "reports"
    evidence = base / "evidence"
    repo = base / "smoke-repo"
    origin = base / "smoke-origin.git"
    for path in (logs, reports, evidence, repo):
        path.mkdir(parents=True, exist_ok=True)
    prepare_log = logs / "prepare.log"

    run_logged(
        ["nix", "flake", "init", "-t", f"path:{ROOT}#{template}"],
        cwd=repo,
        log=prepare_log,
    )
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", "OpenCode Runtime Smoke"],
        ["git", "config", "user.email", "opencode-runtime-smoke@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-m", "Initialize runtime smoke fixture"],
    ):
        run_logged(command, cwd=repo, log=prepare_log)

    run_logged(
        ["nix", "develop", "--command", "just", "project::bootstrap", "smoke-project"],
        cwd=repo,
        log=prepare_log,
    )
    if template == "agent-python":
        run_logged(
            ["nix", "develop", "--command", "just", "project::python::sync"],
            cwd=repo,
            log=prepare_log,
        )
    run_logged(["git", "add", "."], cwd=repo, log=prepare_log)
    run_logged(
        ["nix", "develop", "--command", "git", "commit", "-m", "Bootstrap runtime smoke fixture"],
        cwd=repo,
        log=prepare_log,
    )

    run_logged(["git", "init", "--bare", str(origin)], cwd=ROOT, log=prepare_log)
    run_logged(["git", "remote", "add", "origin", str(origin)], cwd=repo, log=prepare_log)
    run_logged(
        ["nix", "develop", "--command", "git", "push", "-u", "origin", "main"],
        cwd=repo,
        log=prepare_log,
    )
    run_logged(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        log=prepare_log,
    )

    for recipe in ("agent::doctor", "agent::context", "project::doctor", "project::check"):
        run_logged(
            ["nix", "develop", "--command", "just", recipe],
            cwd=repo,
            log=prepare_log,
        )
    dirty = run_capture(["git", "status", "--porcelain"], cwd=repo)
    if dirty:
        raise RuntimeSmokeError(f"fixture is dirty after initialization:\n{dirty}")

    for task, slug, purpose, scope, acceptance, test_plan in TASK_DEFINITIONS:
        run_logged(
            ["nix", "develop", "--command", "just", "agent::task-start", task, slug],
            cwd=repo,
            log=prepare_log,
        )
        state = repo / ".worktrees" / f"{task}-{slug}" / ".task-state" / "task.md"
        write_contract(
            state,
            purpose=purpose,
            scope=scope,
            acceptance=acceptance,
            test_plan=test_plan,
        )

    metadata = {
        "issue": issue,
        "template": template,
        "templatesCommit": run_capture(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "opencodeVersion": run_capture(["opencode", "--version"], cwd=ROOT),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "smokeRepo": str(repo),
        "origin": str(origin),
    }
    (base / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = reports / "REPORT.md"
    report.write_text(report_template(issue, metadata), encoding="utf-8")
    return {
        "status": "PASS",
        **metadata,
        "report": str(report),
        "prepareLog": str(prepare_log),
    }


def status(issue: str) -> dict:
    base = workspace(issue)
    repo = smoke_repo(issue)
    if not repo.is_dir():
        raise RuntimeSmokeError(f"missing prepared smoke repository: {repo}")
    return {
        "issue": issue,
        "workspace": str(base),
        "smokeRepo": str(repo),
        "gitStatus": run_capture(["git", "status", "--porcelain"], cwd=repo).splitlines(),
        "worktrees": run_capture(["git", "worktree", "list", "--porcelain"], cwd=repo).splitlines(),
        "report": str(base / "reports" / "REPORT.md"),
        "logs": str(base / "logs"),
        "evidence": str(base / "evidence"),
    }


def snapshot_sessions(issue: str, label: str) -> dict:
    label = validate_name(label, "snapshot label")
    repo = smoke_repo(issue)
    if not repo.is_dir():
        raise RuntimeSmokeError(f"missing prepared smoke repository: {repo}")
    output = run_capture(["opencode", "session", "list", "--format", "json"], cwd=repo)
    destination = workspace(issue) / "evidence" / f"sessions-{label}.json"
    destination.write_text(output + "\n", encoding="utf-8")
    return {"status": "PASS", "path": str(destination)}


def export_session(issue: str, session_id: str) -> dict:
    validate_name(session_id, "session id")
    repo = smoke_repo(issue)
    if not repo.is_dir():
        raise RuntimeSmokeError(f"missing prepared smoke repository: {repo}")
    result = subprocess.run(
        ["opencode", "export", "--sanitize", session_id],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeSmokeError(f"opencode export failed: {detail}")
    destination = workspace(issue) / "evidence" / f"session-{session_id}.json"
    destination.write_text(result.stdout, encoding="utf-8")
    return {"status": "PASS", "path": str(destination), "sanitized": True}


def report(issue: str) -> dict:
    path = workspace(issue) / "reports" / "REPORT.md"
    if not path.is_file():
        raise RuntimeSmokeError(f"missing runtime report: {path}")
    return {"report": str(path)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Repository-local OpenCode runtime smoke harness")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")

    command = sub.add_parser("prepare")
    command.add_argument("--issue", default="issue-41")
    command.add_argument("--template", default="agent-python")

    command = sub.add_parser("status")
    command.add_argument("--issue", default="issue-41")

    command = sub.add_parser("snapshot-sessions")
    command.add_argument("--issue", default="issue-41")
    command.add_argument("--label", required=True)

    command = sub.add_parser("export-session")
    command.add_argument("--issue", default="issue-41")
    command.add_argument("--session-id", required=True)

    command = sub.add_parser("report")
    command.add_argument("--issue", default="issue-41")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            value = doctor()
        elif args.command == "prepare":
            value = prepare(args.issue, args.template)
        elif args.command == "status":
            value = status(args.issue)
        elif args.command == "snapshot-sessions":
            value = snapshot_sessions(args.issue, args.label)
        elif args.command == "export-session":
            value = export_session(args.issue, args.session_id)
        elif args.command == "report":
            value = report(args.issue)
        else:
            raise AssertionError(args.command)
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except RuntimeSmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
