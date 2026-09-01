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
DOWNSTREAM_REPO = "upiscium/AgentKnowledgeVault"
DEFAULT_BRANCH = "main"
EVIDENCE_ROOT_NAME = ".agent-core-release-gate/evidence"

SHA_RE = re.compile(r"[0-9a-f]{40}")
PR_RE = re.compile(r"[1-9][0-9]{0,8}")
OPERATION_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")

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


def source_root() -> Path:
    tool_root = Path(__file__).resolve().parent.parent
    root_text = git(["rev-parse", "--show-toplevel"], tool_root).strip()
    root = Path(root_text).resolve()
    if root != tool_root:
        raise GateError("release-gate tool must be installed at the source checkout root")
    return root


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
        "ci": "PASS",
        "status": "READY_FOR_DOGFOOD",
    }


def worktree_records(root: Path) -> dict[Path, dict[str, Any]]:
    records: dict[Path, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in git(["worktree", "list", "--porcelain"], root).splitlines() + [""]:
        if line.startswith("worktree "):
            if current is not None:
                records[current["path"]] = current
            current = {"path": Path(line.removeprefix("worktree ")).resolve(), "detached": False}
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif current is not None and line == "detached":
            current["detached"] = True
        elif not line and current is not None:
            records[current["path"]] = current
            current = None
    return records


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
    path = safe_worktree_path(root, destination, create_parents=True)
    records = worktree_records(root)
    registered = records.get(path.resolve())
    if registered is not None:
        if registered.get("head") != checked["head"] or not registered.get("detached"):
            raise GateError("existing worktree is bound to another revision or branch")
        verify_worktree(path, checked["head"], checked["tree"])
    else:
        if path.exists():
            raise GateError("existing destination is not a registered worktree")
        object_type = git(["cat-file", "-t", checked["head"]], root, check=False).strip()
        if object_type != "commit":
            git(["fetch", "--no-tags", "origin", checked["head"]], root)
        if git(["cat-file", "-t", checked["head"]], root).strip() != "commit":
            raise GateError("candidate revision is not an immutable Git commit")
        local_tree = git(["rev-parse", f"{checked['head']}^{{tree}}"], root).strip()
        if local_tree != checked["tree"]:
            raise GateError("local candidate object has an unexpected tree")
        git(["worktree", "add", "--detach", str(path), checked["head"]], root)
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
    expected_keys = {
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
    if set(value) != expected_keys:
        raise GateError("evidence record has an unexpected schema")
    if value["schema"] != "agent-core-release-gate" or value["version"] != 1:
        raise GateError("evidence schema version is unsupported")
    if value["repo"] != CANONICAL_REPO or value["downstreamRepo"] != DOWNSTREAM_REPO:
        raise GateError("evidence repository binding is invalid")
    if isinstance(value["pr"], bool) or not isinstance(value["pr"], int) or value["pr"] <= 0:
        raise GateError("evidence pull request is invalid")
    valid_sha(value["head"], "evidence head")
    valid_sha(value["tree"], "evidence tree")
    positive_optional_integer(value["task"], "Task ID")
    positive_optional_integer(value["downstreamPr"], "downstream PR")
    if value["outcome"] != "PASS":
        raise GateError("evidence outcome is not PASS")
    if not isinstance(value["operation"], str) or not OPERATION_RE.fullmatch(value["operation"]):
        raise GateError("evidence operation is invalid")
    validate_timestamp(value["recordedAt"])
    return value


def load_evidence(root: Path, *, create: bool = False) -> list[dict[str, Any]]:
    directory = evidence_root(root, create=create)
    records: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        metadata = path.lstat()
        if (
            path.suffix != ".json"
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise GateError(f"unsafe or unexpected evidence entry: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GateError(f"invalid evidence record: {path.name}") from exc
        records.append(validate_evidence_payload(payload))
    return records


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
        "version": 1,
        "repo": CANONICAL_REPO,
        "pr": checked["pr"],
        "head": checked["head"],
        "tree": checked["tree"],
        "downstreamRepo": DOWNSTREAM_REPO,
        "task": task_id,
        "downstreamPr": downstream_pr_id,
        "outcome": "PASS",
        "operation": operation,
        "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    validate_evidence_payload(payload)
    subject = hashlib.sha256(
        f"{CANONICAL_REPO}:{checked['pr']}:{checked['head']}:{checked['tree']}:{DOWNSTREAM_REPO}".encode()
    ).hexdigest()
    directory = evidence_root(root, create=False)
    # The deterministic subject path makes the check-and-create operation
    # atomic across concurrent recorders. O_EXCL permits exactly one winner.
    target = directory / f"{subject}.json"
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
    status = gh_status(["api", f"repos/{CANONICAL_REPO}/releases/tags/{version}"], root)
    if status == 200:
        raise GateError("release already exists for the supplied version")
    if status != 404:
        raise GateError(f"release lookup returned unexpected HTTP status {status}")


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

    checked = candidate(root, number, require_open=False)
    if checked["head"] != dogfood_head or checked["tree"] != dogfood_tree:
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

    records = load_evidence(root)
    matching = [
        item
        for item in records
        if item["pr"] == checked["pr"]
        and item["head"] == dogfood_head
        and item["tree"] == dogfood_tree
    ]
    if len(matching) != 1:
        raise GateError("exact PASS dogfood evidence is missing, duplicated, or conflicting")
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
    return {
        "pr": checked["pr"],
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
    root = source_root()
    if arguments.command == "release-candidate-check":
        output = candidate(root, arguments.pr)
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
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
