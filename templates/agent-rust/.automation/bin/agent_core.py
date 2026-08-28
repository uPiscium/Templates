#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import task_lifecycle as lifecycle

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SAFE_CHECK_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}


class AutomationError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    remove_env: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in [key for key in environment if key.startswith("GIT_")]:
        environment.pop(name, None)
    for name in remove_env:
        environment.pop(name, None)
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, env=environment
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AutomationError(f"{' '.join(command)}: {detail}")
    return result


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def gh(*args: str, cwd: Path | None = None) -> str:
    return run(["gh", *args], cwd=cwd, remove_env=("GH_REPO",)).stdout.strip()


def repo_root(cwd: Path | None = None) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()


def common_git_dir(root: Path) -> Path:
    value = Path(git("rev-parse", "--git-common-dir", cwd=root))
    return value if value.is_absolute() else (root / value).resolve()


def current_branch(root: Path) -> str:
    branch = git("branch", "--show-current", cwd=root)
    if not branch:
        raise AutomationError("detached HEAD is not supported")
    return branch


def default_branch(root: Path) -> str:
    symbolic = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=root, check=False)
    if symbolic.startswith("origin/"):
        return symbolic.removeprefix("origin/")
    try:
        data = json.loads(gh("repo", "view", "--json", "defaultBranchRef", cwd=root))
        name = data.get("defaultBranchRef", {}).get("name")
        if name:
            return name
    except (AutomationError, json.JSONDecodeError):
        pass
    raise AutomationError("cannot resolve default branch; configure origin/HEAD or GitHub CLI access")


def validate_task(task: str) -> None:
    if not TASK_RE.fullmatch(task):
        raise AutomationError(f"invalid Task ID: {task!r}")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.fullmatch(slug):
        raise AutomationError(f"invalid Task slug: {slug!r}")


def ensure_task_branch(root: Path, task: str) -> str:
    validate_task(task)
    branch = current_branch(root)
    if task not in branch or not (branch.startswith("task/") or branch.startswith("fix/")):
        raise AutomationError(f"current branch {branch!r} is not the Task branch for {task}")
    if branch == default_branch(root):
        raise AutomationError("Task operation refused on the default branch")
    return branch


def policy(root: Path) -> dict:
    path = root / ".automation" / "policy.toml"
    if tomllib is None:
        raise AutomationError("Python 3.11+ is required to parse policy.toml")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutomationError(f"missing policy: {path}") from exc


def matches_protected(path: str, patterns: list[str]) -> bool:
    for raw in patterns:
        if raw.endswith("/**") and (path == raw[:-3] or path.startswith(raw[:-2])):
            return True
        if path == raw:
            return True
    return False


def pending_paths(root: Path) -> list[str]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = git(*args, cwd=root)
        paths.update(line for line in output.splitlines() if line)
    return sorted(paths)


def reject_unsafe_paths(root: Path, paths: list[str]) -> None:
    cfg = policy(root)
    protected = cfg.get("paths", {}).get("automation_core", [])
    secret_names = cfg.get("paths", {}).get("secret_patterns", [])
    bad_core = [path for path in paths if matches_protected(path, protected)]
    if bad_core:
        raise AutomationError("ordinary Task modifies Automation Core: " + ", ".join(bad_core))
    lowered = [(path, path.lower()) for path in paths]
    bad_secret = [path for path, low in lowered if any(token.lower() in low for token in secret_names)]
    if bad_secret:
        raise AutomationError("potential secret file in Task changes: " + ", ".join(bad_secret))
    if any(path == ".task-state" or path.startswith(".task-state/") for path in paths):
        raise AutomationError(".task-state must never be committed")


def ensure_task_state_excluded(root: Path) -> None:
    exclude = common_git_dir(root) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if "/.task-state/" not in lines:
        with exclude.open("a", encoding="utf-8") as handle:
            if lines and lines[-1] != "":
                handle.write("\n")
            handle.write("/.task-state/\n")


def task_state_path(root: Path) -> Path:
    return root / ".task-state" / "task.md"


def write_task_state(worktree: Path, task: str, branch: str, base: str, base_revision: str) -> None:
    template = worktree / ".automation" / "templates" / "task-state.md"
    state_dir = worktree / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8")
    replacements = {
        "@@TASK_ID@@": task,
        "@@BRANCH@@": branch,
        "@@WORKTREE@@": str(worktree),
        "@@BASE_BRANCH@@": base,
        "@@BASE_REVISION@@": base_revision,
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    (state_dir / "task.md").write_text(text, encoding="utf-8")


def task_start(root: Path, task: str, slug: str) -> None:
    validate_task(task)
    validate_slug(slug)
    base = default_branch(root)
    base_ref = f"refs/remotes/origin/{base}"
    base_revision = git("rev-parse", "--verify", base_ref, cwd=root, check=False) or git("rev-parse", base, cwd=root)
    branch = f"task/{task}-{slug}"
    worktree = root / ".worktrees" / f"{task}-{slug}"
    if worktree.exists():
        raise AutomationError(f"worktree path already exists: {worktree}")
    if git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root, check=False) == "":
        result = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False)
        if result.returncode == 0:
            raise AutomationError(f"branch already exists: {branch}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "-b", branch, str(worktree), base_revision], cwd=root)
    ensure_task_state_excluded(worktree)
    write_task_state(worktree, task, branch, base, base_revision)
    print(json.dumps({"task": task, "branch": branch, "worktree": str(worktree), "base": base, "baseRevision": base_revision}))


def context(root: Path) -> dict:
    branch = current_branch(root)
    state = task_state_path(root)
    return {
        "repositoryRoot": str(root),
        "worktree": str(root),
        "branch": branch,
        "defaultBranch": default_branch(root),
        "taskState": str(state) if state.exists() else None,
    }


def doctor(root: Path) -> None:
    missing = [tool for tool in ("git", "gh", "just", "python3") if shutil.which(tool) is None]
    if missing:
        raise AutomationError("missing required tools: " + ", ".join(missing))
    if not (root / ".automation" / "policy.toml").is_file():
        raise AutomationError("missing .automation/policy.toml")
    if not (root / "just" / "project" / "mod.just").is_file():
        raise AutomationError("missing Project Adapter: just/project/mod.just")
    ensure_task_state_excluded(root)
    print("Agent Core doctor: PASS")


def status(root: Path, task: str) -> None:
    branch = ensure_task_branch(root, task)
    print(json.dumps({"task": task, "branch": branch, "status": git("status", "--short", cwd=root).splitlines()}))


def verify(root: Path, task: str) -> None:
    ensure_task_branch(root, task)
    run(["just", "project::check"], cwd=root)
    print("Project verification: PASS")


def commit_task(root: Path, task: str, message: str) -> None:
    ensure_task_branch(root, task)
    paths = pending_paths(root)
    if not paths:
        raise AutomationError("no Task changes to commit")
    reject_unsafe_paths(root, paths)
    run(["git", "add", "--", *paths], cwd=root)
    run(["git", "diff", "--cached", "--check"], cwd=root)
    staged = git("diff", "--cached", "--name-only", cwd=root).splitlines()
    reject_unsafe_paths(root, staged)
    commit_message = message.strip() or f"task: {task}"
    if task not in commit_message:
        commit_message = f"{commit_message}\n\nTask: {task}"
    run(["git", "commit", "-m", commit_message], cwd=root)
    print(git("rev-parse", "HEAD", cwd=root))


def push_task(root: Path, task: str) -> None:
    branch = ensure_task_branch(root, task)
    run(["git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"], cwd=root)
    print(f"pushed origin/{branch}")


def pr_for_branch(root: Path, branch: str) -> dict | None:
    result = run(["gh", "pr", "view", branch, "--json", "number,title,body,headRefName,baseRefName,isDraft,state,headRefOid"], cwd=root, check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def pr_body(root: Path, task: str) -> Path:
    state_dir = root / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "pr-body.md"
    if not path.exists():
        template = root / ".automation" / "templates" / "pull-request.md"
        text = template.read_text(encoding="utf-8").replace("@@TASK_ID@@", task)
        path.write_text(text, encoding="utf-8")
    return path


def pr_create(root: Path, task: str) -> None:
    branch = ensure_task_branch(root, task)
    if pr_for_branch(root, branch):
        raise AutomationError(f"pull request already exists for {branch}")
    base = default_branch(root)
    title = f"{task}: {branch.split('/', 1)[1]}"
    body = pr_body(root, task)
    gh("pr", "create", "--draft", "--base", base, "--head", branch, "--title", title, "--body-file", str(body), cwd=root)
    print(json.dumps(pr_for_branch(root, branch)))


def pr_edit(root: Path, task: str) -> None:
    branch = ensure_task_branch(root, task)
    pr = pr_for_branch(root, branch)
    if not pr:
        raise AutomationError(f"no pull request for {branch}")
    title_file = root / ".task-state" / "pr-title.txt"
    title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else pr["title"]
    body = pr_body(root, task)
    gh("pr", "edit", str(pr["number"]), "--title", title, "--body-file", str(body), cwd=root)
    print(f"updated PR #{pr['number']}")


def pr_ready(root: Path, task: str) -> None:
    verify(root, task)
    branch = ensure_task_branch(root, task)
    pr = pr_for_branch(root, branch)
    if not pr:
        raise AutomationError(f"no pull request for {branch}")
    gh("pr", "ready", str(pr["number"]), cwd=root)
    print(f"PR #{pr['number']} marked ready")


def cleanup(root: Path, task: str) -> None:
    validate_task(task)
    branch = current_branch(root)
    if task not in branch:
        raise AutomationError("cleanup must run from the Task worktree")
    pr = pr_for_branch(root, branch)
    if not pr or pr.get("state") != "MERGED":
        raise AutomationError("cleanup refused until the Task PR is merged")
    print("cleanup must be executed by the Main worktree in the lifecycle extension (#8)")


def pr_details(root: Path, pr: str) -> dict:
    data = json.loads(gh("pr", "view", pr, "--json", "number,baseRefName,headRefName,headRefOid,isDraft,isCrossRepository,mergeCommit,mergeable,statusCheckRollup,state", cwd=root))
    return data


def validate_integration(root: Path, pr: str) -> dict:
    data = pr_details(root, pr)
    if data["baseRefName"] != default_branch(root):
        raise AutomationError("PR base is not the repository default branch")
    if data["isDraft"]:
        raise AutomationError("Draft PR cannot be merged")
    if data.get("mergeable") != "MERGEABLE":
        raise AutomationError(f"PR is not mergeable: {data.get('mergeable')}")
    failures = []
    for check in data.get("statusCheckRollup") or []:
        conclusion = check.get("conclusion")
        status = check.get("status")
        if status and status != "COMPLETED":
            failures.append(check.get("name") or check.get("context") or "pending check")
        elif conclusion and conclusion not in SAFE_CHECK_CONCLUSIONS:
            failures.append(check.get("name") or check.get("context") or conclusion)
    if failures:
        raise AutomationError("required checks are not successful: " + ", ".join(failures))
    return data


def integration_checkpoint(root: Path, pr: str) -> Path:
    path = common_git_dir(root) / "opencode" / "integration" / f"pr-{pr}.head"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def integrate_check(root: Path, pr: str) -> None:
    data = validate_integration(root, pr)
    integration_checkpoint(root, pr).write_text(data["headRefOid"] + "\n", encoding="utf-8")
    print(json.dumps({"pr": data["number"], "head": data["headRefOid"], "status": "verified"}))


def integrate_merge(root: Path, pr: str) -> None:
    checkpoint = integration_checkpoint(root, pr)
    if not checkpoint.exists():
        raise AutomationError("run integrate::check before merge")
    expected = checkpoint.read_text(encoding="utf-8").strip()
    data = validate_integration(root, pr)
    if data["headRefOid"] != expected:
        raise AutomationError(f"PR head moved after integration check: expected {expected}, got {data['headRefOid']}")
    gh("pr", "merge", pr, "--squash", "--match-head-commit", expected, cwd=root)
    print(f"merged PR #{pr} at {expected}")


def validate_pr_number(pr: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", pr):
        raise AutomationError(f"invalid pull request number: {pr!r}")
    return int(pr)


def prs_for_branch(root: Path, branch: str) -> list[dict]:
    try:
        value = json.loads(
            gh(
                "pr",
                "list",
                "--state",
                "all",
                "--head",
                branch,
                "--limit",
                "100",
                "--json",
                "number,headRefName,baseRefName",
                cwd=root,
            )
        )
    except json.JSONDecodeError as exc:
        raise AutomationError("invalid pull request list returned by GitHub") from exc
    if not isinstance(value, list):
        raise AutomationError("invalid pull request list returned by GitHub")
    return [item for item in value if item.get("headRefName") == branch]


def merged_pr_evidence(root: Path, task: str, pr: str) -> tuple[lifecycle.WorktreeRecord, dict]:
    requested = validate_pr_number(pr)
    try:
        lifecycle.require_main_worktree(root)
        record = lifecycle.worktree_for_task(root, task)
        lifecycle.assert_task_identity(record, task)
        status = lifecycle.state_status(lifecycle.state_path(record.path))
    except lifecycle.LifecycleError as exc:
        raise AutomationError(str(exc)) from exc
    if status not in {"integration-pending", "merged"}:
        raise AutomationError(
            "post-merge finalization requires Task status integration-pending or merged; "
            f"found {status}"
        )
    branch = record.branch
    if branch is None:
        raise AutomationError("registered Task worktree is detached")
    data = pr_details(root, pr)
    base = default_branch(root)
    if data.get("number") != requested:
        raise AutomationError("GitHub returned a different pull request")
    if data.get("state") != "MERGED":
        raise AutomationError("pull request is not merged")
    if data.get("headRefName") != branch:
        raise AutomationError("pull request head does not match the registered Task branch")
    if data.get("baseRefName") != base:
        raise AutomationError("pull request base is not the repository default branch")
    if data.get("isCrossRepository"):
        raise AutomationError("cross-repository pull requests cannot finalize a local Task")
    merge_oid = (data.get("mergeCommit") or {}).get("oid")
    if not isinstance(merge_oid, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_oid):
        raise AutomationError("merged pull request has no valid merge commit identity")
    matches = prs_for_branch(root, branch)
    if len(matches) != 1 or matches[0].get("number") != requested:
        raise AutomationError("Task pull request identity is missing or ambiguous")
    return record, data


def merge_commit_is_ancestor(root: Path, merge_oid: str, revision: str) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_oid):
        return False
    return run(
        ["git", "merge-base", "--is-ancestor", merge_oid, revision],
        cwd=root,
        check=False,
    ).returncode == 0


def integrate_finalize(root: Path, task: str, pr: str) -> None:
    validate_task(task)
    record, evidence = merged_pr_evidence(root, task, pr)
    merge_oid = evidence["mergeCommit"]["oid"]
    try:
        synchronized = lifecycle.synchronize_default_branch(root)
    except lifecycle.LifecycleError as exc:
        raise AutomationError(str(exc)) from exc
    revision = synchronized["revision"]
    if not merge_commit_is_ancestor(root, merge_oid, revision):
        raise AutomationError(
            "GitHub merge commit is not present in the synchronized default branch"
        )

    # Re-read GitHub evidence after the fetch/update boundary before granting
    # terminal-state authority.
    current_record, current = merged_pr_evidence(root, task, pr)
    def fingerprint(value: dict) -> tuple:
        return (
            value.get("number"),
            value.get("state"),
            value.get("headRefName"),
            value.get("baseRefName"),
            (value.get("mergeCommit") or {}).get("oid"),
            value.get("isCrossRepository"),
        )
    if current_record != record or fingerprint(current) != fingerprint(evidence):
        raise AutomationError("pull request or Task identity changed during finalization")
    if not merge_commit_is_ancestor(root, merge_oid, revision):
        raise AutomationError("merge identity changed during finalization")
    try:
        lifecycle.require_synchronized_default_branch_revision(
            root, synchronized["branch"], revision
        )
        outcome = lifecycle.mark_task_merged_from_integration(record, task)
    except lifecycle.LifecycleError as exc:
        raise AutomationError(str(exc)) from exc
    print(
        json.dumps(
            {
                "task": task,
                "pr": evidence["number"],
                "branch": synchronized["branch"],
                "revision": revision,
                "mergeCommit": merge_oid,
                "status": outcome,
            }
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    scope = parser.add_subparsers(dest="scope", required=True)
    agent = scope.add_parser("agent")
    agent_cmd = agent.add_subparsers(dest="command", required=True)
    for name in ("doctor", "context"):
        agent_cmd.add_parser(name)
    start = agent_cmd.add_parser("task-start"); start.add_argument("task"); start.add_argument("slug")
    for name in ("status", "verify", "push", "pr-create", "pr-edit", "pr-ready", "cleanup"):
        p = agent_cmd.add_parser(name); p.add_argument("task")
    commit = agent_cmd.add_parser("commit"); commit.add_argument("task"); commit.add_argument("message", nargs="?", default="")
    integrate = scope.add_parser("integrate")
    integrate_cmd = integrate.add_subparsers(dest="command", required=True)
    for name in ("check", "merge"):
        p = integrate_cmd.add_parser(name); p.add_argument("pr")
    finalize = integrate_cmd.add_parser("finalize")
    finalize.add_argument("task")
    finalize.add_argument("pr")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        root = repo_root()
        if args.scope == "agent":
            actions = {
                "doctor": lambda: doctor(root),
                "context": lambda: print(json.dumps(context(root), indent=2)),
                "task-start": lambda: task_start(root, args.task, args.slug),
                "status": lambda: status(root, args.task),
                "verify": lambda: verify(root, args.task),
                "commit": lambda: commit_task(root, args.task, args.message),
                "push": lambda: push_task(root, args.task),
                "pr-create": lambda: pr_create(root, args.task),
                "pr-edit": lambda: pr_edit(root, args.task),
                "pr-ready": lambda: pr_ready(root, args.task),
                "cleanup": lambda: cleanup(root, args.task),
            }
        else:
            actions = {
                "check": lambda: integrate_check(root, args.pr),
                "merge": lambda: integrate_merge(root, args.pr),
                "finalize": lambda: integrate_finalize(root, args.task, args.pr),
            }
        actions[args.command]()
        return 0
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
