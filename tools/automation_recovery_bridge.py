#!/usr/bin/env python3
"""Narrow Templates-root bridge for source-side maintenance recovery."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
import secrets
import subprocess


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PATH = ROOT / "tools" / "automation_recovery_bridge.py"
ENGINE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
CONTRACT_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_contract.py"
LIFECYCLE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py"
_TRUSTED_GIT: Path | None = None
_TRUSTED_GH: Path | None = None
_REVISION_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class BridgeError(RuntimeError):
    pass


class BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BridgeError(message)


def trusted_git() -> Path:
    global _TRUSTED_GIT
    if _TRUSTED_GIT is not None:
        return _TRUSTED_GIT
    candidate = shutil.which("git")
    if not candidate:
        raise BridgeError("trusted Git executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise BridgeError("trusted Git executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BridgeError(f"Git executable is not root-owned and immutable: {executable}")
    _TRUSTED_GIT = executable
    return executable


def trusted_gh() -> Path:
    global _TRUSTED_GH
    if _TRUSTED_GH is not None:
        return _TRUSTED_GH
    candidate = shutil.which("gh")
    if not candidate:
        raise BridgeError("trusted GitHub CLI executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise BridgeError("trusted GitHub CLI executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BridgeError(f"GitHub CLI executable is not root-owned and immutable: {executable}")
    _TRUSTED_GH = executable
    return executable


def trusted_gh_run(command: list[str], **kwargs):
    if not command or command[0] != "gh":
        raise BridgeError("Task Contract GitHub runner only accepts gh commands")
    environment = dict(kwargs.pop("env", os.environ))
    for key in list(environment):
        if key in {"GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"}:
            environment.pop(key, None)
    return subprocess.run([str(trusted_gh()), *command[1:]], env=environment, **kwargs)


def git_bytes(args: list[str], *, cwd: Path) -> bytes:
    if not args or args[0] != "git":
        raise BridgeError("bootstrap Git helper only accepts Git commands")
    environment = {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [str(trusted_git()), "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null", "--no-pager", *args[1:]]
    result = subprocess.run(command, cwd=cwd,
                            capture_output=True, env=environment, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise BridgeError(f"{' '.join(args)}: {detail}")
    return result.stdout


def _revision(root: Path) -> str:
    value = git_bytes(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=root)
    revision = value.decode("ascii", "strict").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise BridgeError("Git HEAD is not a full lowercase commit revision")
    return revision


def _clean_root(root: Path, expected_revision: str | None = None) -> str:
    top = git_bytes(["git", "rev-parse", "--show-toplevel"], cwd=root).decode("utf-8", "strict").strip()
    if Path(top).resolve() != root:
        raise BridgeError(f"recovery bridge must run from the Templates Git root: {top}")
    revision = _revision(root)
    if expected_revision is not None and revision != expected_revision:
        raise BridgeError("Templates HEAD changed during bootstrap")
    if git_bytes(["git", "status", "--porcelain=v1", "-z"], cwd=root):
        raise BridgeError("Templates source worktree must be clean")
    return revision


def _tree_blob(root: Path, revision: str, relative: str) -> tuple[str, int]:
    if not _REVISION_RE.fullmatch(revision):
        raise BridgeError("Git tree lookup requires a full lowercase commit revision")
    raw = git_bytes(["git", "ls-tree", "-z", revision, "--", relative], cwd=root)
    records = raw.split(b"\0")
    records = [record for record in records if record]
    matches: list[tuple[str, int]] = []
    for record in records:
        header, separator, path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3 or path.decode("utf-8", "surrogateescape") != relative:
            continue
        mode, kind, oid = fields
        if kind != b"blob" or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) or mode not in (b"100644", b"100755"):
            raise BridgeError(f"Git tree entry for {relative} is not a safe regular blob")
        matches.append((oid.decode("ascii"), int(mode, 8)))
    if len(matches) != 1:
        raise BridgeError(f"Git revision must contain exactly one regular blob at {relative}")
    return matches[0]


def _blob(root: Path, oid: str) -> bytes:
    return git_bytes(["git", "cat-file", "blob", oid], cwd=root)


def _verify_bootstrap(root: Path, revision: str) -> None:
    if BOOTSTRAP_PATH != Path(__file__).resolve():
        raise BridgeError("bootstrap path is not exactly the expected Templates path")
    oid, mode = _tree_blob(root, revision, "tools/automation_recovery_bridge.py")
    try:
        metadata = BOOTSTRAP_PATH.lstat()
        live = BOOTSTRAP_PATH.read_bytes()
    except OSError as exc:
        raise BridgeError("cannot read the live recovery bootstrap") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o111 != mode & 0o111:
        raise BridgeError("live recovery bootstrap is not a regular file with the HEAD executable mode")
    if live != _blob(root, oid):
        raise BridgeError("live recovery bootstrap does not match its HEAD blob")


@contextmanager
def _verified_task_contract(root: Path, revision: str):
    """Load the contract and its dependency exclusively from HEAD Git blobs."""
    contract_oid, contract_mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/task_contract.py")
    lifecycle_oid, lifecycle_mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/task_lifecycle.py")
    with tempfile.TemporaryDirectory(prefix="automation-contract-") as directory:
        directory_path = Path(directory)
        lifecycle_path = directory_path / "task_lifecycle.py"
        contract_path = directory_path / "task_contract.py"
        lifecycle_path.write_bytes(_blob(root, lifecycle_oid))
        contract_path.write_bytes(_blob(root, contract_oid))
        # The Git modes are validated by _tree_blob; the private copies need
        # not retain executable bits and must not be writable by other users.
        os.chmod(lifecycle_path, 0o600)
        os.chmod(contract_path, 0o600)
        lifecycle_name = f"_templates_verified_lifecycle_{secrets.token_hex(16)}"
        contract_name = f"_templates_verified_contract_{secrets.token_hex(16)}"
        previous_lifecycle = sys.modules.get("task_lifecycle")
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            lifecycle_spec = importlib.util.spec_from_file_location(lifecycle_name, lifecycle_path)
            if lifecycle_spec is None or lifecycle_spec.loader is None:
                raise BridgeError("cannot create specification for verified Task Contract dependency")
            lifecycle = importlib.util.module_from_spec(lifecycle_spec)
            sys.modules[lifecycle_name] = lifecycle
            sys.modules["task_lifecycle"] = lifecycle
            lifecycle_spec.loader.exec_module(lifecycle)

            def trusted_lifecycle_run(command, *, cwd=None, check=True):
                if not command or command[0] != "git":
                    raise BridgeError("verified Task Contract attempted a non-Git lifecycle command")
                environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                    "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
                result = subprocess.run(
                    [str(trusted_git()), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                     "-c", "core.pager=", *command[1:]], cwd=cwd, text=True,
                    capture_output=True, env=environment)
                if check and result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                    raise lifecycle.LifecycleError(f"{' '.join(command)}: {detail}")
                return result

            lifecycle.run = trusted_lifecycle_run
            contract_spec = importlib.util.spec_from_file_location(contract_name, contract_path)
            if contract_spec is None or contract_spec.loader is None:
                raise BridgeError("cannot create specification for verified Task Contract")
            contract = importlib.util.module_from_spec(contract_spec)
            sys.modules[contract_name] = contract
            contract_spec.loader.exec_module(contract)
            yield contract
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"cannot load verified Task Contract: {exc}") from exc
        finally:
            sys.modules.pop(contract_name, None)
            sys.modules.pop(lifecycle_name, None)
            if previous_lifecycle is None:
                sys.modules.pop("task_lifecycle", None)
            else:
                sys.modules["task_lifecycle"] = previous_lifecycle
            sys.dont_write_bytecode = previous_bytecode


@contextmanager
def _verified_engine(root: Path, revision: str):
    oid, mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/automation_upgrade.py")
    engine_bytes = _blob(root, oid)
    _clean_root(root, revision)
    with tempfile.TemporaryDirectory(prefix="automation-bridge-") as directory:
        path = Path(directory) / "engine.py"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(engine_bytes)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        name = f"_templates_verified_engine_{secrets.token_hex(16)}"
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        spec = None
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise BridgeError("cannot create specification for verified recovery engine")
            engine = importlib.util.module_from_spec(spec)
            sys.modules[name] = engine
            spec.loader.exec_module(engine)
            yield engine
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"cannot load verified recovery engine: {exc}") from exc
        finally:
            sys.modules.pop(name, None)
            sys.dont_write_bytecode = previous


@contextmanager
def maintenance_environment():
    previous = dict(os.environ)
    os.environ["AUTOMATION_MAINTENANCE"] = "1"
    for key in list(os.environ):
        if key.startswith("GIT_") or key in {"GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"}:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def parser() -> argparse.ArgumentParser:
    result = BoundedArgumentParser(description="Templates source-side maintenance recovery bridge")
    sub = result.add_subparsers(dest="command", required=True, parser_class=BoundedArgumentParser)
    recover = sub.add_parser("recover-maintenance-authority")
    recover.add_argument("target", type=Path)
    commit = sub.add_parser("commit-recovered-maintenance")
    commit.add_argument("target", type=Path)
    commit.add_argument("task")
    commit.add_argument("message", nargs="?", default="")
    issue = sub.add_parser("recover-task-contract-from-issue")
    issue.add_argument("target", type=Path)
    issue.add_argument("issue", type=_issue_argument)
    rebind = sub.add_parser("rebind-maintenance-provenance")
    rebind.add_argument("target", type=Path)
    rebind.add_argument("expected_source_revision", type=_revision_argument)
    resume = sub.add_parser("resume-contract-check")
    resume.add_argument("target", type=Path)
    resume.add_argument("task", type=_issue_argument)
    return result


def _error_text(exc: BaseException) -> str:
    detail = str(exc).replace("\r", "\\r").replace("\n", "\\n")
    detail = re.sub(r"[^\x20-\x7e]", "?", detail)
    return detail[:1600] or exc.__class__.__name__


def _issue_argument(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise BridgeError("Issue number must be an exact positive decimal integer")
    return value


def _revision_argument(value: str) -> str:
    if not _REVISION_RE.fullmatch(value):
        raise BridgeError("source revision must be a full lowercase immutable Git object ID")
    return value


def _check_resume_contract(contract, target: Path, task: str) -> dict:
    """Check the explicitly named registered Task, without changing it."""
    target_root = contract.lifecycle.repo_root(target)
    if target_root != target:
        raise BridgeError("resume contract check target must be an exact Git worktree root")
    current = contract.lifecycle.current_worktree(target)
    main = contract.lifecycle.main_worktree(target)
    if current.path != target or current.path == main.path:
        raise BridgeError("resume contract check target must be the exact registered Task worktree")
    record = contract.lifecycle.worktree_for_task(target, task)
    if record.path != target:
        raise BridgeError("resume contract check target is not the exact registered Task worktree")
    result = contract.check_resume_contract(target, task, runner=trusted_gh_run)
    if result.get("worktree") != str(target):
        raise BridgeError("canonical resume contract resolved a different Task worktree")
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if Path(__file__).resolve() != BOOTSTRAP_PATH:
            raise BridgeError("bootstrap path is not exactly the expected Templates path")
        revision = _clean_root(ROOT)
        _verify_bootstrap(ROOT, revision)
        _clean_root(ROOT, revision)
        target = args.target.resolve()
        with maintenance_environment():
            if args.command in {"recover-task-contract-from-issue", "resume-contract-check"}:
                trusted_git()
                with _verified_task_contract(ROOT, revision) as contract:
                    _clean_root(ROOT, revision)
                    if args.command == "recover-task-contract-from-issue":
                        value = contract.recover_task_from_issue(target, args.issue, runner=trusted_gh_run)
                        result = {"status": "TASK_CONTRACT_RECOVERED", **value}
                    else:
                        result = _check_resume_contract(contract, target, args.task)
                        result["implementationRevision"] = revision
            else:
                with _verified_engine(ROOT, revision) as engine:
                    if engine.git_executable().resolve() != trusted_git():
                        raise BridgeError("recovery engine selected a different Git executable")
                    _clean_root(ROOT, revision)
                    if args.command == "recover-maintenance-authority":
                        result = engine.recover_maintenance_authority_from_source(
                            target, ROOT, expected_implementation_revision=revision)
                    elif args.command == "commit-recovered-maintenance":
                        result = engine.commit_recovered_maintenance(
                            target, ROOT, args.task, args.message, expected_implementation_revision=revision)
                    elif args.command == "rebind-maintenance-provenance":
                        result = engine.rebind_maintenance_provenance_from_source(
                            target, ROOT, args.expected_source_revision,
                            expected_implementation_revision=revision)
                    else:  # pragma: no cover
                        raise BridgeError(f"unsupported bridge command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {_error_text(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
