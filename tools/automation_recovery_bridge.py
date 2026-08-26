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
_TRUSTED_GIT: Path | None = None
_REVISION_RE = re.compile(r"[0-9a-f]{40,64}")


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
        if kind != b"blob" or not re.fullmatch(rb"[0-9a-f]{40,64}", oid) or mode not in (b"100644", b"100755"):
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
    previous = os.environ.get("AUTOMATION_MAINTENANCE")
    os.environ["AUTOMATION_MAINTENANCE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AUTOMATION_MAINTENANCE", None)
        else:
            os.environ["AUTOMATION_MAINTENANCE"] = previous


def parser() -> argparse.ArgumentParser:
    result = BoundedArgumentParser(description="Templates source-side maintenance recovery bridge")
    sub = result.add_subparsers(dest="command", required=True, parser_class=BoundedArgumentParser)
    recover = sub.add_parser("recover-maintenance-authority")
    recover.add_argument("target", type=Path)
    commit = sub.add_parser("commit-recovered-maintenance")
    commit.add_argument("target", type=Path)
    commit.add_argument("task")
    commit.add_argument("message", nargs="?", default="")
    return result


def _error_text(exc: BaseException) -> str:
    detail = str(exc).replace("\r", "\\r").replace("\n", "\\n")
    detail = re.sub(r"[^\x20-\x7e]", "?", detail)
    return detail[:1600] or exc.__class__.__name__


def main() -> int:
    try:
        args = parser().parse_args()
        if Path(__file__).resolve() != BOOTSTRAP_PATH:
            raise BridgeError("bootstrap path is not exactly the expected Templates path")
        revision = _clean_root(ROOT)
        _verify_bootstrap(ROOT, revision)
        with _verified_engine(ROOT, revision) as engine:
            if engine.git_executable().resolve() != trusted_git():
                raise BridgeError("recovery engine selected a different Git executable")
            _clean_root(ROOT, revision)
            target = args.target.resolve()
            with maintenance_environment():
                if args.command == "recover-maintenance-authority":
                    result = engine.recover_maintenance_authority_from_source(
                        target, ROOT, expected_implementation_revision=revision)
                elif args.command == "commit-recovered-maintenance":
                    result = engine.commit_recovered_maintenance(
                        target, ROOT, args.task, args.message, expected_implementation_revision=revision)
                else:  # pragma: no cover
                    raise BridgeError(f"unsupported bridge command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {_error_text(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
