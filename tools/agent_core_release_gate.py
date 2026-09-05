#!/usr/bin/env python3
"""Narrow operator API for Agent Core release-candidate dogfood gates."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any


CANONICAL_REPO = "upiscium/Templates"
CANONICAL_GIT_URL = "https://github.com/upiscium/Templates.git"
CANONICAL_WORKFLOW_PATH = ".github/workflows/template-ci.yml"
DOWNSTREAM_REPO = "upiscium/AgentKnowledgeVault"
DEFAULT_BRANCH = "main"
EVIDENCE_ROOT_NAME = ".agent-core-release-gate/evidence"
GATE_TOOL_RELATIVE = "tools/agent_core_release_gate.py"
GATE_LAUNCHER_RELATIVE = "tools/run_agent_core_release_gate.sh"

# This is the sole legacy exception.  It describes the v1 receipt emitted by
# issue #117; v1 is otherwise only a loadable file format, never a trust rule.
ISSUE_117_COMPATIBILITY = {
    "pr": 117,
    "head": "9eb86882bbb569aeae356c59e7d459e9fd8ba4f9",
    "tree": "7c5c6fb0bd5e0e881510e256f0b76e95c304c9c5",
    "merge": "67fbc64e1127c19b9e424ab99dff4626f67be63b",
    "workflowId": 329870494,
    "workflowPath": CANONICAL_WORKFLOW_PATH,
    "runId": 33584314368,
    "runAttempt": 1,
    "evidenceSha256": "317dd08ea4840dac4a820043630315ece7c098e13ff77c992af63568442190ce",
}

SHA_RE = re.compile(r"[0-9a-f]{40}")
PR_RE = re.compile(r"[1-9][0-9]{0,8}")
OPERATION_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
CANONICAL_REMOTE_RE = re.compile(
    r"https://github\.com/upiscium/Templates(?:\.git)?",
    re.IGNORECASE,
)
UNSAFE_LOCAL_CONFIG_RE = re.compile(
    r"(?:include(?:if)?\..*|url\..*|http\..*|credential\..*|filter\..*|protocol\..*|"
    r"core\.(?:gitproxy|hookspath|sshcommand|worktree)|"
    r"remote\..+\.(?:proxy|proxyauthmethod|receivepack|uploadpack|vcs))",
    re.IGNORECASE,
)

_TRUSTED_EXECUTABLES: dict[str, Path] = {}


class GateError(RuntimeError):
    """A fail-closed release-gate error."""


class BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise GateError(message)


def trusted_executable(name: str) -> Path:
    cached = _TRUSTED_EXECUTABLES.get(name)
    if cached is not None:
        return cached
    candidate = shutil.which(name)
    if not candidate:
        raise GateError(f"trusted {name} executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise GateError(f"trusted {name} executable is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or executable.name != name
    ):
        raise GateError(f"{name} executable is not root-owned and immutable: {executable}")
    _TRUSTED_EXECUTABLES[name] = executable
    return executable


def sanitized_environment() -> dict[str, str]:
    environment = {}
    for key in ("HOME", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN", "NO_COLOR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    environment.update(
        {
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def command(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not argv or argv[0] not in {"git", "gh"}:
        raise GateError("release-gate runner only accepts Git or GitHub CLI commands")
    executable = trusted_executable(argv[0])
    command_argv = [str(executable), *argv[1:]]
    if argv[0] == "git":
        command_argv = [
            str(executable),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=",
            *argv[1:],
        ]
    result = subprocess.run(
        command_argv,
        cwd=cwd,
        env=sanitized_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GateError(f"{argv[0]} command failed: {detail}")
    return result


def git(args: list[str], cwd: Path, *, check: bool = True) -> str:
    return command(["git", "--no-pager", *args], cwd, check=check).stdout


def git_bytes(args: list[str], cwd: Path, *, check: bool = True) -> bytes:
    executable = trusted_executable("git")
    result = subprocess.run(
        [
            str(executable),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=",
            "--no-pager",
            *args,
        ],
        cwd=cwd,
        env=sanitized_environment(),
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise GateError(f"git command failed: {detail}")
    return result.stdout


def gh(args: list[str], cwd: Path) -> Any:
    if not args or args[0] != "api":
        raise GateError("GitHub runner only accepts API reads")
    text = command(["gh", "api", "--hostname", "github.com", *args[1:]], cwd).stdout
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError("GitHub API returned invalid JSON") from exc


def gh_status(args: list[str], cwd: Path) -> int:
    if not args or args[0] != "api":
        raise GateError("GitHub status runner only accepts API reads")
    result = command(
        ["gh", "api", "--hostname", "github.com", *args[1:], "--include"],
        cwd,
        check=False,
    )
    statuses = re.findall(r"^HTTP/\S+\s+(\d{3})\b", result.stdout, re.MULTILINE)
    if not statuses:
        detail = result.stderr.strip() or "no HTTP status"
        raise GateError(f"GitHub API status query failed: {detail}")
    return int(statuses[-1])


def source_blob(root: Path, revision: str, relative: str) -> tuple[str, int]:
    if not SHA_RE.fullmatch(revision):
        raise GateError("source implementation revision is invalid")
    raw = git_bytes(["ls-tree", "-z", revision, "--", relative], root)
    records = [record for record in raw.split(b"\0") if record]
    matches: list[tuple[str, int]] = []
    for record in records:
        header, separator, path = record.partition(b"\t")
        fields = header.split()
        if (
            not separator
            or len(fields) != 3
            or path.decode("utf-8", "surrogateescape") != relative
        ):
            continue
        mode, kind, oid = fields
        if kind != b"blob" or mode not in {b"100644", b"100755"} or not SHA_RE.fullmatch(
            oid.decode("ascii", "strict")
        ):
            raise GateError(f"source implementation path is not a safe regular Git blob: {relative}")
        matches.append((oid.decode("ascii"), int(mode, 8)))
    if len(matches) != 1:
        raise GateError(f"source implementation must contain exactly one Git blob: {relative}")
    return matches[0]


def verify_live_source_blob(root: Path, revision: str, relative: str, live_path: Path) -> None:
    expected_path = root / relative
    if live_path.resolve() != expected_path.resolve():
        raise GateError(f"live source implementation path is unexpected: {relative}")
    oid, git_mode = source_blob(root, revision, relative)
    try:
        metadata = expected_path.lstat()
        live_content = expected_path.read_bytes()
    except OSError as exc:
        raise GateError(f"cannot read live source implementation: {relative}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(stat.S_IMODE(metadata.st_mode) & 0o111) != bool(git_mode & 0o111)
    ):
        raise GateError(f"live source implementation type or mode differs from HEAD: {relative}")
    if live_content != git_bytes(["cat-file", "blob", oid], root):
        raise GateError(f"live source implementation content differs from HEAD: {relative}")


def verify_source_implementation(
    root: Path,
    *,
    expected_head: str | None = None,
    tool_path: Path | None = None,
    launcher_path: Path | None = None,
) -> str:
    root = root.resolve()
    head = git(["rev-parse", "--verify", "HEAD^{commit}"], root).strip()
    valid_sha(head, "source implementation HEAD")
    if expected_head is not None and head != expected_head:
        raise GateError("source implementation HEAD moved during the operation")
    verify_live_source_blob(
        root,
        head,
        GATE_TOOL_RELATIVE,
        tool_path or root / GATE_TOOL_RELATIVE,
    )
    verify_live_source_blob(
        root,
        head,
        GATE_LAUNCHER_RELATIVE,
        launcher_path or root / GATE_LAUNCHER_RELATIVE,
    )
    if git_bytes(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root):
        raise GateError("Templates source worktree must be clean")
    if git(["rev-parse", "--verify", "HEAD^{commit}"], root).strip() != head:
        raise GateError("source implementation HEAD moved during verification")
    return head


def source_root() -> tuple[Path, str]:
    tool_root = Path(__file__).resolve().parent.parent
    root_text = git(["rev-parse", "--show-toplevel"], tool_root).strip()
    root = Path(root_text).resolve()
    if root != tool_root:
        raise GateError("release-gate tool must be installed at the source checkout root")
    head = verify_source_implementation(
        root,
        tool_path=Path(__file__),
        launcher_path=root / GATE_LAUNCHER_RELATIVE,
    )
    return root, head


def valid_pr(value: str) -> str:
    if not PR_RE.fullmatch(value):
        raise GateError("invalid pull request number")
    return value


def valid_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise GateError(f"invalid {name}")
    return value


def pull_request(root: Path, number: str) -> dict[str, Any]:
    value = gh(["api", f"repos/{CANONICAL_REPO}/pulls/{valid_pr(number)}"], root)
    if not isinstance(value, dict):
        raise GateError("GitHub returned an invalid pull request response")
    return value


def validate_pr_identity(pr: dict[str, Any], *, require_open: bool) -> str:
    head = (pr.get("head") or {}).get("sha")
    valid_sha(head, "pull request head")
    state = pr.get("state")
    if require_open and state != "open":
        raise GateError("pull request is not open")
    if not require_open and state not in {"open", "closed"}:
        raise GateError("pull request state is invalid")
    if (pr.get("base") or {}).get("ref") != DEFAULT_BRANCH:
        raise GateError("pull request base is not the expected default branch")
    if ((pr.get("base") or {}).get("repo") or {}).get("full_name") != CANONICAL_REPO:
        raise GateError("pull request base repository is not canonical")
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != CANONICAL_REPO:
        raise GateError("cross-repository pull requests are not release candidates")
    return head


def check_rollup(root: Path, number: str, expected_head: str) -> None:
    query = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      commits(last:1){nodes{commit{
        oid
        statusCheckRollup{contexts(first:100){
          pageInfo{hasNextPage}
          nodes{
            __typename
            ... on CheckRun {status conclusion}
            ... on StatusContext {state}
          }
        }}
      }}}
    }
  }
}
""".strip()
    data = gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            "owner=upiscium",
            "-F",
            "name=Templates",
            "-F",
            f"number={number}",
        ],
        root,
    )
    try:
        commit = data["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
        contexts_object = commit["statusCheckRollup"]["contexts"]
        contexts = contexts_object["nodes"]
        has_next = contexts_object["pageInfo"]["hasNextPage"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GateError("statusCheckRollup is unavailable") from exc
    if commit.get("oid") != expected_head:
        raise GateError("CI evidence is stale for a different pull request head")
    if has_next:
        raise GateError("statusCheckRollup has unexamined contexts")
    if not isinstance(contexts, list) or not contexts:
        raise GateError("statusCheckRollup is empty")
    for context in contexts:
        if not isinstance(context, dict):
            raise GateError("statusCheckRollup contains an invalid context")
        if context.get("__typename") == "CheckRun":
            if context.get("status") != "COMPLETED" or context.get("conclusion") != "SUCCESS":
                raise GateError("CI is incomplete or unsuccessful")
        elif context.get("__typename") == "StatusContext":
            if context.get("state") != "SUCCESS":
                raise GateError("CI is incomplete or unsuccessful")
        else:
            raise GateError("statusCheckRollup contains an unknown context type")


def workflow_run_subject(
    run: Any,
    workflow_id: int,
    expected_head: str,
    expected_pr: int,
    *,
    allow_empty_pull_requests: bool = False,
) -> tuple[int, int]:
    if not isinstance(run, dict):
        raise GateError("Template CI workflow run is invalid")
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt <= 0
    ):
        raise GateError("Template CI workflow run identity is invalid")
    if (
        run.get("workflow_id") != workflow_id
        or run.get("path") != CANONICAL_WORKFLOW_PATH
        or run.get("event") != "pull_request"
        or run.get("head_sha") != expected_head
    ):
        raise GateError("Template CI workflow run is not bound to the exact candidate")
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list) or (
        len(pull_requests) != 1
        and not (allow_empty_pull_requests and not pull_requests)
    ):
        raise GateError("Template CI workflow run has ambiguous pull request identity")
    if not pull_requests:
        return run_id, attempt
    associated = pull_requests[0]
    if (
        not isinstance(associated, dict)
        or associated.get("number") != expected_pr
        or (associated.get("head") or {}).get("sha") != expected_head
        or (associated.get("head") or {}).get("repo", {}).get("url")
        != f"https://api.github.com/repos/{CANONICAL_REPO}"
        or (associated.get("base") or {}).get("ref") != DEFAULT_BRANCH
        or (associated.get("base") or {}).get("repo", {}).get("url")
        != f"https://api.github.com/repos/{CANONICAL_REPO}"
    ):
        raise GateError("Template CI workflow run belongs to another pull request")
    return run_id, attempt


def exact_template_ci_run(
    root: Path,
    workflow_id: int,
    expected_head: str,
    expected_pr: int,
) -> dict[str, Any]:
    response = gh(
        [
            "api",
            f"repos/{CANONICAL_REPO}/actions/workflows/{workflow_id}/runs"
            f"?event=pull_request&head_sha={expected_head}&per_page=100",
        ],
        root,
    )
    if not isinstance(response, dict):
        raise GateError("Template CI workflow runs response is invalid")
    runs = response.get("workflow_runs")
    total = response.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int) or not isinstance(runs, list):
        raise GateError("Template CI workflow runs response is invalid")
    if total != len(runs):
        raise GateError("Template CI workflow run pagination is incomplete")
    if total == 0:
        raise GateError("Template CI workflow is missing for the exact candidate")
    if total != 1:
        raise GateError("Template CI workflow run is ambiguous for the exact candidate")
    workflow_run_subject(runs[0], workflow_id, expected_head, expected_pr)
    return runs[0]


def check_template_ci(root: Path, expected_head: str, expected_pr: int) -> dict[str, int | str]:
    workflow = gh(
        ["api", f"repos/{CANONICAL_REPO}/actions/workflows/template-ci.yml"],
        root,
    )
    if not isinstance(workflow, dict):
        raise GateError("canonical Template CI workflow metadata is invalid")
    workflow_id = workflow.get("id")
    if (
        isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id <= 0
        or workflow.get("path") != CANONICAL_WORKFLOW_PATH
        or workflow.get("state") != "active"
    ):
        raise GateError("canonical Template CI workflow is missing or inactive")

    first = exact_template_ci_run(root, workflow_id, expected_head, expected_pr)
    run_id, attempt = workflow_run_subject(first, workflow_id, expected_head, expected_pr)
    detail = gh(["api", f"repos/{CANONICAL_REPO}/actions/runs/{run_id}"], root)
    if workflow_run_subject(detail, workflow_id, expected_head, expected_pr) != (run_id, attempt):
        raise GateError("Template CI workflow attempt changed during validation")
    if detail.get("status") != "completed" or detail.get("conclusion") != "success":
        raise GateError("Template CI workflow is incomplete or unsuccessful")

    final = exact_template_ci_run(root, workflow_id, expected_head, expected_pr)
    if workflow_run_subject(final, workflow_id, expected_head, expected_pr) != (run_id, attempt):
        raise GateError("Template CI workflow run changed during validation")
    return {
        "workflowId": workflow_id,
        "workflowPath": CANONICAL_WORKFLOW_PATH,
        "runId": run_id,
        "runAttempt": attempt,
    }


def commit_tree(root: Path, commit: str, name: str = "commit") -> str:
    response = gh(["api", f"repos/{CANONICAL_REPO}/git/commits/{commit}"], root)
    if not isinstance(response, dict) or response.get("sha") != commit:
        raise GateError(f"GitHub did not return the exact {name}")
    return valid_sha((response.get("tree") or {}).get("sha"), f"{name} tree")


def candidate(root: Path, number: str, *, require_open: bool = True) -> dict[str, Any]:
    number = valid_pr(number)
    first = pull_request(root, number)
    head = validate_pr_identity(first, require_open=require_open)
    tree = commit_tree(root, head, "candidate commit")
    template_ci = check_template_ci(root, head, int(number))
    check_rollup(root, number, head)
    final = pull_request(root, number)
    final_head = validate_pr_identity(final, require_open=require_open)
    if final_head != head:
        raise GateError("pull request head moved during validation")
    return {
        "pr": int(number),
        "head": head,
        "tree": tree,
        "base": DEFAULT_BRANCH,
        **template_ci,
        "ci": "PASS",
        "status": "READY_FOR_DOGFOOD",
    }


def release_ready_check(root: Path, number: str) -> dict[str, Any]:
    """Assert that the open candidate has exactly one current v2 dogfood receipt."""
    checked = candidate(root, number)
    records = load_evidence_records(root, create=False)
    matching = [
        (record, digest)
        for record, digest in records
        if record["pr"] == checked["pr"]
        and record["head"] == checked["head"]
        and record["tree"] == checked["tree"]
    ]
    if len(matching) != 1:
        raise GateError("exact current dogfood evidence is missing, duplicated, or stale")
    evidence, evidence_digest = matching[0]
    if evidence["version"] != 2:
        raise GateError("release-ready evidence must use schema version 2")
    for field in ("workflowId", "workflowPath", "runId", "runAttempt"):
        if evidence[field] != checked[field]:
            raise GateError(f"release-ready evidence {field} does not match the candidate")

    final = candidate(root, number)
    if final != checked:
        raise GateError("candidate changed during release-ready validation")
    return {
        **checked,
        "status": "READY_FOR_MERGE",
        "evidenceSha256": evidence_digest,
    }


def validate_git_execution_configuration(root: Path) -> None:
    raw_names = git(
        ["config", "--local", "--no-includes", "--null", "--name-only", "--list"],
        root,
    )
    names = [item for item in raw_names.split("\0") if item]
    for name in names:
        if UNSAFE_LOCAL_CONFIG_RE.fullmatch(name):
            raise GateError(f"unsafe local Git configuration is forbidden: {name}")

    if any(name.lower() == "extensions.worktreeconfig" for name in names):
        worktree_config = git(
            ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"],
            root,
        ).strip()
        if worktree_config != "true":
            raise GateError("extensions.worktreeConfig must be a valid true boolean")
        raw_worktree_names = git(
            ["config", "--worktree", "--no-includes", "--null", "--name-only", "--list"],
            root,
        )
        for name in (item for item in raw_worktree_names.split("\0") if item):
            if UNSAFE_LOCAL_CONFIG_RE.fullmatch(name):
                raise GateError(f"unsafe worktree Git configuration is forbidden: {name}")


def validate_local_git_configuration(root: Path) -> str:
    validate_git_execution_configuration(root)

    raw_urls = git(["remote", "get-url", "--all", "origin"], root)
    urls = [line for line in raw_urls.splitlines() if line]
    if len(urls) != 1 or not CANONICAL_REMOTE_RE.fullmatch(urls[0]):
        raise GateError("origin is not the canonical Templates HTTPS repository")
    return urls[0]


def ensure_owned_directory(path: Path, *, create: bool, private: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise GateError(f"required directory is missing: {path}")
        try:
            path.mkdir(mode=0o700 if private else 0o755)
        except FileExistsError:
            pass
        metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & (0o077 if private else 0o022)
    ):
        raise GateError(f"directory is unsafe: {path}")


def safe_worktree_path(root: Path, destination: str, *, create_parents: bool) -> Path:
    base = root / ".worktrees"
    raw = Path(destination)
    if raw.is_absolute():
        selected = raw
    elif raw.parts and raw.parts[0] == ".worktrees":
        selected = root / raw
    else:
        selected = base / raw
    absolute = Path(os.path.abspath(selected))
    try:
        relative = absolute.relative_to(base)
    except ValueError as exc:
        raise GateError("worktree path must be under the source checkout .worktrees directory") from exc
    if not relative.parts:
        raise GateError("worktree path cannot be the .worktrees directory itself")

    if base.exists() or base.is_symlink():
        ensure_owned_directory(base, create=False, private=False)
    elif create_parents:
        ensure_owned_directory(base, create=True, private=False)

    cursor = base
    for component in relative.parts[:-1]:
        cursor = cursor / component
        if cursor.exists() or cursor.is_symlink():
            ensure_owned_directory(cursor, create=False, private=False)
        elif create_parents:
            ensure_owned_directory(cursor, create=True, private=False)
    if absolute.is_symlink():
        raise GateError("worktree destination cannot be a symlink")
    if absolute.resolve(strict=False) != absolute:
        raise GateError("worktree path cannot traverse symlinks")
    return absolute


def verify_worktree(path: Path, expected_head: str, expected_tree: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise GateError("worktree destination is not a safe directory")
    top = Path(git(["rev-parse", "--show-toplevel"], path).strip()).resolve()
    if top != path.resolve():
        raise GateError("destination is not the exact registered worktree root")
    if git(["rev-parse", "--verify", "HEAD^{commit}"], path).strip() != expected_head:
        raise GateError("worktree is bound to another revision")
    if git(["rev-parse", "--verify", "HEAD^{tree}"], path).strip() != expected_tree:
        raise GateError("worktree tree does not match the candidate tree")
    if git(["symbolic-ref", "-q", "HEAD"], path, check=False).strip():
        raise GateError("release-candidate worktree is not detached")
    if git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path):
        raise GateError("release-candidate worktree is not clean")


def create_or_verify_worktree(root: Path, number: str, destination: str) -> dict[str, Any]:
    checked = candidate(root, number)
    validate_local_git_configuration(root)
    path = safe_worktree_path(root, destination, create_parents=True)
    if path.exists():
        validate_git_execution_configuration(path)
        verify_worktree(path, checked["head"], checked["tree"])
    else:
        path.mkdir(mode=0o700)
        git(["init", "--quiet", "--initial-branch=main", "."], path)
        validate_git_execution_configuration(path)
        git(["fetch", "--no-tags", CANONICAL_GIT_URL, checked["head"]], path)
        validate_git_execution_configuration(path)
        if git(["cat-file", "-t", checked["head"]], path).strip() != "commit":
            raise GateError("candidate revision is not an immutable Git commit")
        local_tree = git(["rev-parse", f"{checked['head']}^{{tree}}"], path).strip()
        if local_tree != checked["tree"]:
            raise GateError("local candidate object has an unexpected tree")
        validate_git_execution_configuration(path)
        git(["checkout", "--quiet", "--detach", checked["head"]], path)
        validate_git_execution_configuration(path)
        verify_worktree(path, checked["head"], checked["tree"])

    final = candidate(root, number)
    if final != checked:
        raise GateError("candidate changed during the worktree operation")
    return {
        **checked,
        "path": str(path),
        "detached": True,
        "clean": True,
    }


def evidence_root(root: Path, *, create: bool) -> Path:
    parent = root / ".agent-core-release-gate"
    evidence = parent / "evidence"
    ensure_owned_directory(parent, create=create, private=True)
    ensure_owned_directory(evidence, create=create, private=True)
    return evidence


def positive_optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GateError(f"evidence {name} is invalid")
    return value


def positive_integer(value: Any, name: str) -> int:
    checked = positive_optional_integer(value, name)
    if checked is None:
        raise GateError(f"evidence {name} is invalid")
    return checked


def validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GateError("evidence timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GateError("evidence timestamp is invalid") from exc
    if parsed.tzinfo != dt.timezone.utc:
        raise GateError("evidence timestamp is not UTC")
    return value


def validate_evidence_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError("evidence record is not a JSON object")
    common_keys = {
        "schema",
        "version",
        "repo",
        "pr",
        "head",
        "tree",
        "downstreamRepo",
        "task",
        "downstreamPr",
        "outcome",
        "operation",
        "recordedAt",
    }
    version = value.get("version")
    if isinstance(version, bool):
        version = None
    identity_keys = {"workflowId", "workflowPath", "runId", "runAttempt"}
    if version == 1:
        expected_keys = common_keys
    elif version == 2:
        expected_keys = common_keys | identity_keys
    else:
        expected_keys = set()
    if set(value) != expected_keys:
        raise GateError("evidence record has an unexpected schema")
    if value["schema"] != "agent-core-release-gate" or version not in {1, 2}:
        raise GateError("evidence schema version is unsupported")
    if value["repo"] != CANONICAL_REPO or value["downstreamRepo"] != DOWNSTREAM_REPO:
        raise GateError("evidence repository binding is invalid")
    if isinstance(value["pr"], bool) or not isinstance(value["pr"], int) or value["pr"] <= 0:
        raise GateError("evidence pull request is invalid")
    valid_sha(value["head"], "evidence head")
    valid_sha(value["tree"], "evidence tree")
    if version == 2:
        positive_integer(value["workflowId"], "workflow ID")
        if value["workflowPath"] != CANONICAL_WORKFLOW_PATH:
            raise GateError("evidence workflow path is invalid")
        positive_integer(value["runId"], "run ID")
        positive_integer(value["runAttempt"], "run attempt")
    positive_optional_integer(value["task"], "Task ID")
    positive_optional_integer(value["downstreamPr"], "downstream PR")
    if value["outcome"] != "PASS":
        raise GateError("evidence outcome is not PASS")
    if not isinstance(value["operation"], str) or not OPERATION_RE.fullmatch(value["operation"]):
        raise GateError("evidence operation is invalid")
    validate_timestamp(value["recordedAt"])
    return value


def evidence_subject(
    repository: str,
    pr: int,
    head: str,
    tree: str,
    downstream_repository: str,
) -> str:
    return hashlib.sha256(
        f"{repository}:{pr}:{head}:{tree}:{downstream_repository}".encode("utf-8")
    ).hexdigest()


def evidence_filename(payload: dict[str, Any]) -> str:
    return (
        evidence_subject(
            payload["repo"],
            payload["pr"],
            payload["head"],
            payload["tree"],
            payload["downstreamRepo"],
        )
        + ".json"
    )


def read_evidence_record(path: Path) -> tuple[dict[str, Any], str]:
    if path.suffix != ".json":
        raise GateError(f"unsafe or unexpected evidence entry: {path.name}")
    try:
        initial_metadata = path.lstat()
    except OSError as exc:
        raise GateError(f"unsafe or unexpected evidence entry: {path.name}") from exc
    if (
        not stat.S_ISREG(initial_metadata.st_mode)
        or stat.S_ISLNK(initial_metadata.st_mode)
        or initial_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(initial_metadata.st_mode) & 0o077
    ):
        raise GateError(f"unsafe or unexpected evidence entry: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GateError(f"unsafe or unexpected evidence entry: {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (metadata.st_dev, metadata.st_ino)
            != (initial_metadata.st_dev, initial_metadata.st_ino)
        ):
            raise GateError(f"unsafe or unexpected evidence entry: {path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GateError(f"invalid evidence record: {path.name}") from exc
    validated = validate_evidence_payload(payload)
    if path.name != evidence_filename(validated):
        raise GateError(f"evidence filename does not match its immutable subject: {path.name}")
    return validated, hashlib.sha256(raw).hexdigest()


def load_evidence_records(
    root: Path, *, create: bool = False
) -> list[tuple[dict[str, Any], str]]:
    directory = evidence_root(root, create=create)
    records: list[tuple[dict[str, Any], str]] = []
    for path in sorted(directory.iterdir()):
        records.append(read_evidence_record(path))
    return records


def load_evidence(root: Path, *, create: bool = False) -> list[dict[str, Any]]:
    return [payload for payload, _digest in load_evidence_records(root, create=create)]


def record_evidence(
    root: Path,
    number: str,
    operation: str,
    task: str,
    downstream_pr: str,
) -> dict[str, Any]:
    if not OPERATION_RE.fullmatch(operation):
        raise GateError("invalid operation identifier")
    task_id = int(task) if task and PR_RE.fullmatch(task) else None
    if task and task_id is None:
        raise GateError("Task ID must be a positive integer")
    downstream_pr_id = int(downstream_pr) if downstream_pr and PR_RE.fullmatch(downstream_pr) else None
    if downstream_pr and downstream_pr_id is None:
        raise GateError("downstream PR must be a positive integer")

    checked = candidate(root, number)
    existing = load_evidence(root, create=True)
    same_subject = [
        item
        for item in existing
        if item["pr"] == checked["pr"]
        and item["head"] == checked["head"]
        and item["tree"] == checked["tree"]
    ]
    if same_subject:
        raise GateError("dogfood evidence for this exact candidate already exists")

    payload = {
        "schema": "agent-core-release-gate",
        "version": 2,
        "repo": CANONICAL_REPO,
        "pr": checked["pr"],
        "head": checked["head"],
        "tree": checked["tree"],
        "workflowId": checked["workflowId"],
        "workflowPath": checked["workflowPath"],
        "runId": checked["runId"],
        "runAttempt": checked["runAttempt"],
        "downstreamRepo": DOWNSTREAM_REPO,
        "task": task_id,
        "downstreamPr": downstream_pr_id,
        "outcome": "PASS",
        "operation": operation,
        "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    validate_evidence_payload(payload)
    directory = evidence_root(root, create=False)
    # The deterministic subject path makes the check-and-create operation
    # atomic across concurrent recorders. O_EXCL permits exactly one winner.
    target = directory / evidence_filename(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise GateError("dogfood evidence for this exact candidate already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return payload


def release_absent(root: Path, version: str) -> None:
    if not VERSION_RE.fullmatch(version):
        raise GateError("invalid release version")
    release_status = gh_status(["api", f"repos/{CANONICAL_REPO}/releases/tags/{version}"], root)
    if release_status == 200:
        raise GateError("release already exists for the supplied version")
    if release_status != 404:
        raise GateError(f"release lookup returned unexpected HTTP status {release_status}")
    tag_status = gh_status(["api", f"repos/{CANONICAL_REPO}/git/ref/tags/{version}"], root)
    if tag_status == 200:
        raise GateError("Git tag already exists for the supplied version")
    if tag_status != 404:
        raise GateError(f"Git tag lookup returned unexpected HTTP status {tag_status}")


def post_merge_template_ci(
    root: Path,
    expected_head: str,
    expected_pr: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Verify the workflow and the exact run named by the selected receipt."""
    workflow = gh(["api", f"repos/{CANONICAL_REPO}/actions/workflows/template-ci.yml"], root)
    if not isinstance(workflow, dict):
        raise GateError("canonical Template CI workflow metadata is invalid")
    workflow_id = workflow.get("id")
    if (
        isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id <= 0
        or workflow.get("path") != CANONICAL_WORKFLOW_PATH
        or workflow.get("state") != "active"
    ):
        raise GateError("canonical Template CI workflow is missing or inactive")

    if evidence["version"] == 2:
        identity = {
            "workflowId": evidence["workflowId"],
            "workflowPath": evidence["workflowPath"],
            "runId": evidence["runId"],
            "runAttempt": evidence["runAttempt"],
        }
    else:
        identity = ISSUE_117_COMPATIBILITY
    if identity["workflowId"] != workflow_id or identity["workflowPath"] != workflow.get("path"):
        raise GateError("recorded Template CI workflow identity is not canonical")
    detail = gh(["api", f"repos/{CANONICAL_REPO}/actions/runs/{identity['runId']}"], root)
    try:
        observed = workflow_run_subject(
            detail,
            workflow_id,
            expected_head,
            expected_pr,
            # An empty list is safe here only because v1 was already gated by
            # the exact issue #117 predicate, while v2 carries immutable CI
            # identity. Arbitrary v1 never reaches this call.
            allow_empty_pull_requests=True,
        )
    except GateError:
        raise
    if observed != (identity["runId"], identity["runAttempt"]):
        raise GateError("recorded Template CI workflow attempt changed during validation")
    if detail.get("status") != "completed" or detail.get("conclusion") != "success":
        raise GateError("Template CI workflow is incomplete or unsuccessful")
    return detail


def evidence_matches_issue_117(
    evidence: dict[str, Any],
    evidence_digest: str,
    pr: int,
    head: str,
    tree: str,
    merge: str,
) -> bool:
    return (
        evidence["version"] == 1
        and pr == ISSUE_117_COMPATIBILITY["pr"]
        and head == ISSUE_117_COMPATIBILITY["head"]
        and tree == ISSUE_117_COMPATIBILITY["tree"]
        and merge == ISSUE_117_COMPATIBILITY["merge"]
        and evidence["pr"] == pr
        and evidence["head"] == head
        and evidence["tree"] == tree
        and evidence_digest == ISSUE_117_COMPATIBILITY["evidenceSha256"]
    )


def validate_post_merge_run_association(
    run: dict[str, Any], *, expected_pr: int, expected_head: str, legacy: bool
) -> None:
    associations = run.get("pull_requests")
    if not isinstance(associations, list):
        raise GateError("Template CI workflow pull request identity is invalid")
    if not associations:
        # v1 reaches this point only after the exact issue #117 binding and
        # merged-PR proof above; v2 has its immutable recorded run identity.
        return
    if len(associations) != 1:
        raise GateError("Template CI workflow has ambiguous pull request identity")
    associated = associations[0]
    if (
        not isinstance(associated, dict)
        or associated.get("number") != expected_pr
        or (associated.get("head") or {}).get("sha") != expected_head
        or (associated.get("head") or {}).get("repo", {}).get("url")
        != f"https://api.github.com/repos/{CANONICAL_REPO}"
        or (associated.get("base") or {}).get("ref") != DEFAULT_BRANCH
        or (associated.get("base") or {}).get("repo", {}).get("url")
        != f"https://api.github.com/repos/{CANONICAL_REPO}"
    ):
        raise GateError("Template CI workflow run belongs to another pull request")


def post_merge_gate(
    root: Path,
    number: str,
    merge_commit: str,
    dogfood_head: str,
    dogfood_tree: str,
    version: str,
) -> dict[str, Any]:
    number = valid_pr(number)
    merge_commit = valid_sha(merge_commit, "merge commit")
    dogfood_head = valid_sha(dogfood_head, "dogfood head")
    dogfood_tree = valid_sha(dogfood_tree, "dogfood tree")

    merged = pull_request(root, number)
    merged_head = validate_pr_identity(merged, require_open=False)
    if merged.get("state") != "closed" or merged.get("merged") is not True:
        raise GateError("pull request is not merged")
    if merged_head != dogfood_head:
        raise GateError("merged pull request head is not the dogfooded head")
    if merged.get("merge_commit_sha") != merge_commit:
        raise GateError("supplied merge commit is not the pull request merge commit")

    checked_head = validate_pr_identity(merged, require_open=False)
    checked_tree = commit_tree(root, checked_head, "candidate commit")
    if checked_head != dogfood_head or checked_tree != dogfood_tree:
        raise GateError("dogfood identity does not match the current pull request candidate")
    merge_tree = commit_tree(root, merge_commit, "merge commit")
    if merge_tree != dogfood_tree:
        raise GateError("merge commit tree does not match the dogfooded tree")

    comparison = gh(
        ["api", f"repos/{CANONICAL_REPO}/compare/{merge_commit}...{DEFAULT_BRANCH}"],
        root,
    )
    if not isinstance(comparison, dict) or comparison.get("status") not in {"identical", "ahead"}:
        raise GateError("current main does not contain the merge commit")

    records = load_evidence_records(root)
    matching = [
        (item, digest)
        for item, digest in records
        if item["pr"] == int(number)
        and item["head"] == dogfood_head
        and item["tree"] == dogfood_tree
    ]
    if len(matching) != 1:
        raise GateError("exact PASS dogfood evidence is missing, duplicated, or conflicting")
    selected, selected_digest = matching[0]
    legacy = selected["version"] == 1
    if legacy and not evidence_matches_issue_117(
        selected,
        selected_digest,
        int(number),
        dogfood_head,
        dogfood_tree,
        merge_commit,
    ):
        raise GateError("legacy evidence is not the guarded issue #117 compatibility binding")
    check_rollup(root, number, dogfood_head)
    detail = post_merge_template_ci(root, dogfood_head, int(number), selected)
    validate_post_merge_run_association(
        detail, expected_pr=int(number), expected_head=dogfood_head, legacy=legacy
    )
    if version:
        release_absent(root, version)

    final = pull_request(root, number)
    if (
        validate_pr_identity(final, require_open=False) != dogfood_head
        or final.get("state") != "closed"
        or final.get("merged") is not True
        or final.get("merge_commit_sha") != merge_commit
    ):
        raise GateError("pull request merge identity changed during release-gate validation")
    final_detail = post_merge_template_ci(root, dogfood_head, int(number), selected)
    validate_post_merge_run_association(
        final_detail, expected_pr=int(number), expected_head=dogfood_head, legacy=legacy
    )
    return {
        "pr": int(number),
        "status": "RELEASE_READY",
        "dogfoodHead": dogfood_head,
        "dogfoodTree": dogfood_tree,
        "mergeCommit": merge_commit,
        "mergeTree": merge_tree,
        "treeMatch": True,
        "ci": "PASS",
    }


def parser() -> argparse.ArgumentParser:
    result = BoundedArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    candidate_parser = commands.add_parser("release-candidate-check")
    candidate_parser.add_argument("pr")
    ready_parser = commands.add_parser("release-ready-check")
    ready_parser.add_argument("pr")
    worktree_parser = commands.add_parser("release-candidate-worktree")
    worktree_parser.add_argument("pr")
    worktree_parser.add_argument("path")
    evidence_parser = commands.add_parser("dogfood-evidence-record")
    evidence_parser.add_argument("pr")
    evidence_parser.add_argument("operation")
    evidence_parser.add_argument("--task", default="")
    evidence_parser.add_argument("--downstream-pr", default="")
    gate_parser = commands.add_parser("release-gate-check")
    gate_parser.add_argument("pr")
    gate_parser.add_argument("merge_commit")
    gate_parser.add_argument("dogfood_head")
    gate_parser.add_argument("dogfood_tree")
    gate_parser.add_argument("--version", default="")
    return result


def main() -> int:
    arguments = parser().parse_args()
    root, implementation_head = source_root()
    if arguments.command == "release-candidate-check":
        output = candidate(root, arguments.pr)
    elif arguments.command == "release-ready-check":
        output = release_ready_check(root, arguments.pr)
    elif arguments.command == "release-candidate-worktree":
        output = create_or_verify_worktree(root, arguments.pr, arguments.path)
    elif arguments.command == "dogfood-evidence-record":
        output = record_evidence(
            root,
            arguments.pr,
            arguments.operation,
            arguments.task,
            arguments.downstream_pr,
        )
    else:
        output = post_merge_gate(
            root,
            arguments.pr,
            arguments.merge_commit,
            arguments.dogfood_head,
            arguments.dogfood_tree,
            arguments.version,
        )
    verify_source_implementation(
        root,
        expected_head=implementation_head,
        tool_path=Path(__file__),
        launcher_path=root / GATE_LAUNCHER_RELATIVE,
    )
    output["implementationHead"] = implementation_head
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
