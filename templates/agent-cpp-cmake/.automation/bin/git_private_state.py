"""Owned, fail-closed storage for Agent Core's Git-private runtime state."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess


NAMESPACE = "agent-core"
LEGACY_NAMESPACE = "opencode"
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PR_RE = re.compile(r"[1-9][0-9]*")
HASH_RE = re.compile(r"[0-9a-f]{64}")
SHARED_DIRS = ("cleanup", "integration", "discard-pristine", "automation-maintenance")
FIXED_AUTHORITY_FILES = {"authority.json", "source-recovery-proof.json"}
LOCK_FILES = {"cleanup.lock", "migration.lock"}
_GIT_EXECUTABLE = "git"


class GitPrivateStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Topology:
    common: Path
    admin: Path

    @property
    def admin_is_common(self) -> bool:
        return self.admin == self.common


@dataclass(frozen=True)
class MigrationFile:
    source: Path
    target: Path
    identity: tuple[int, int]
    mode: int
    content: bytes


def _git_path(root: Path, argument: str) -> Path:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    result = subprocess.run(
        [_GIT_EXECUTABLE, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-c", "core.pager=", "rev-parse", argument],
        cwd=root, text=True, capture_output=True, check=False, env=environment,
    )
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        raise GitPrivateStateError(f"cannot resolve Git private-state location: {argument}")
    candidate = Path(raw)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitPrivateStateError(f"cannot resolve Git private-state location: {argument}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise GitPrivateStateError(f"Git private-state location is not a directory: {resolved}")
    return resolved


def topology(root: Path) -> Topology:
    return Topology(_git_path(root, "--git-common-dir"), _git_path(root, "--absolute-git-dir"))


def common_git_dir(root: Path) -> Path:
    return topology(root).common


def admin_git_dir(root: Path) -> Path:
    return topology(root).admin


def common_state(root: Path) -> Path:
    return common_git_dir(root) / NAMESPACE


def admin_maintenance(root: Path) -> Path:
    return admin_git_dir(root) / NAMESPACE / "automation-maintenance"


def cleanup_receipt(root: Path, task: str) -> Path:
    if TASK_RE.fullmatch(task) is None:
        raise GitPrivateStateError("invalid Task ID")
    return common_state(root) / "cleanup" / f"{task}.json"


def discard_receipt(root: Path, task: str) -> Path:
    if TASK_RE.fullmatch(task) is None:
        raise GitPrivateStateError("invalid Task ID")
    return common_state(root) / "discard-pristine" / f"{task}.json"


def integration_checkpoint(root: Path, pr: str) -> Path:
    if PR_RE.fullmatch(pr) is None:
        raise GitPrivateStateError("invalid pull request number")
    return common_state(root) / "integration" / f"pr-{pr}.head"


def _lstat(path: Path, what: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise GitPrivateStateError(f"cannot inspect {what}: {path}") from exc


def _require_dir(path: Path, what: str = "private-state directory") -> os.stat_result:
    metadata = _lstat(path, what)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GitPrivateStateError(f"unsafe {what}: {path}")
    return metadata


def _require_regular(path: Path, what: str) -> os.stat_result:
    metadata = _lstat(path, what)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GitPrivateStateError(f"unsafe {what}: {path}")
    return metadata


def safe_directory(path: Path) -> None:
    _require_dir(path)


def _open_dir(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitPrivateStateError(f"cannot open private-state directory: {path}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise GitPrivateStateError(f"unsafe private-state directory: {path}")
    return descriptor


def _open_anchored_parent(path: Path) -> int:
    """Open a state parent without following namespace or descendant symlinks."""
    parts = path.parts
    indexes = [index for index, part in enumerate(parts) if part in {NAMESPACE, LEGACY_NAMESPACE}]
    if not indexes:
        raise GitPrivateStateError(f"path is outside a recognized private-state namespace: {path}")
    marker = indexes[-1]
    boundary = Path(*parts[:marker])
    descriptor = _open_dir(boundary)
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for component in parts[marker:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise GitPrivateStateError(f"unsafe private-state directory component: {component}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except GitPrivateStateError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise GitPrivateStateError(f"cannot traverse private-state directory: {path.parent}") from exc


def _ensure_child_directory(parent: Path, name: str) -> Path:
    descriptor = _open_dir(parent)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        child = os.open(name, flags, dir_fd=descriptor)
        try:
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise GitPrivateStateError(f"unsafe private-state directory: {parent / name}")
        finally:
            os.close(child)
    except OSError as exc:
        raise GitPrivateStateError(f"cannot create private-state directory: {parent / name}") from exc
    finally:
        os.close(descriptor)
    return parent / name


def _ensure_namespace(base: Path, subdirectories: tuple[str, ...]) -> Path:
    _require_dir(base, "Git administrative directory")
    descriptor = _open_dir(base)
    current = base
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for name in (NAMESPACE, *subdirectories):
            try:
                os.mkdir(name, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=descriptor)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise GitPrivateStateError(f"unsafe private-state directory: {current / name}")
            os.close(descriptor)
            descriptor = child
            current /= name
        return current
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot create private-state namespace below: {base}") from exc
    finally:
        os.close(descriptor)


def read_bytes_identity(
    path: Path, what: str = "private-state record"
) -> tuple[bytes, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = _open_anchored_parent(path)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise GitPrivateStateError(f"unsafe {what}: {path}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (
                after.st_dev, after.st_ino, after.st_size
            ):
                raise GitPrivateStateError(f"{what} changed while reading: {path}")
            return b"".join(chunks), (metadata.st_dev, metadata.st_ino)
        finally:
            os.close(descriptor)
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot read {what}: {path}") from exc
    finally:
        os.close(parent_fd)


def read_bytes(path: Path, what: str = "private-state record") -> bytes:
    return read_bytes_identity(path, what)[0]


def _valid_shared_file(directory: str, name: str, *, allow_fixed: bool) -> bool:
    if directory in {"cleanup", "discard-pristine"}:
        return name.endswith(".json") and TASK_RE.fullmatch(name[:-5]) is not None
    if directory == "integration":
        return re.fullmatch(r"pr-[1-9][0-9]*\.head", name) is not None
    if directory == "automation-maintenance":
        return (
            re.fullmatch(r"[0-9a-f]{64}\.json", name) is not None
            or (allow_fixed and name in FIXED_AUTHORITY_FILES)
        )
    return False


def _legacy_json(content: bytes, path: Path) -> dict:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitPrivateStateError(f"invalid legacy private-state record: {path}") from exc
    if not isinstance(value, dict):
        raise GitPrivateStateError(f"invalid legacy private-state record: {path}")
    return value


def _valid_oid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_task_id(value: object) -> bool:
    return isinstance(value, str) and TASK_RE.fullmatch(value) is not None


def _valid_nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_absolute_path(value: object) -> bool:
    return _valid_nonempty(value) and Path(value).is_absolute()


def _valid_authority_fields(value: dict) -> bool:
    return (
        _valid_task_id(value.get("task_id"))
        and _valid_nonempty(value.get("branch"))
        and _valid_absolute_path(value.get("worktree"))
        and _valid_digest(value.get("authority_nonce"))
        and _valid_digest(value.get("receipt_sha256"))
    )


def _validate_legacy_content(path: Path, content: bytes) -> None:
    directory = path.parent.name
    name = path.name
    if directory == "integration":
        try:
            text = content.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitPrivateStateError(f"invalid legacy integration checkpoint: {path}") from exc
        if not text.endswith("\n") or not _valid_oid(text[:-1]):
            raise GitPrivateStateError(f"invalid legacy integration checkpoint: {path}")
        return
    value = _legacy_json(content, path)
    if directory == "cleanup":
        required = {"schema_version", "task", "status", "worktree", "branch", "local_head", "evidence"}
        valid = (
            set(value) == required
            and value.get("schema_version") == 1
            and value.get("task") == name[:-5]
            and value.get("status") in {"merged", "cancelled"}
            and isinstance(value.get("worktree"), str)
            and isinstance(value.get("branch"), str)
            and _valid_oid(value.get("local_head"))
            and isinstance(value.get("evidence"), dict)
        )
    elif directory == "discard-pristine":
        required = {
            "schema_version", "operation", "task", "status", "worktree", "branch",
            "base_branch", "base_revision", "local_head", "repository",
        }
        valid = (
            set(value) == required
            and value.get("schema_version") == 1
            and value.get("operation") == "discard-pristine"
            and value.get("task") == name[:-5]
            and value.get("status") == "initialized"
            and all(isinstance(value.get(field), str) for field in (
                "worktree", "branch", "base_branch", "repository"
            ))
            and _valid_oid(value.get("base_revision"))
            and value.get("local_head") == value.get("base_revision")
        )
    elif name == "source-recovery-proof.json":
        required = {
            "schema_version", "kind", "task_id", "branch", "worktree", "authority_head",
            "authority_nonce", "receipt_sha256", "receipt_bytes_sha256", "changed_paths_sha256",
            "path_fingerprints_sha256", "implementation_source", "implementation_revision",
            "receipt_source", "receipt_source_revision",
        }
        valid = (
            set(value) == required
            and value.get("schema_version") == 1
            and value.get("kind") == "source-recovery-proof"
            and _valid_authority_fields(value)
            and _valid_oid(value.get("authority_head"))
            and all(_valid_digest(value.get(field)) for field in (
                "receipt_bytes_sha256", "changed_paths_sha256", "path_fingerprints_sha256",
            ))
            and _valid_absolute_path(value.get("implementation_source"))
            and _valid_oid(value.get("implementation_revision"))
            and _valid_absolute_path(value.get("receipt_source"))
            and _valid_oid(value.get("receipt_source_revision"))
        )
    else:
        standard = {"schema_version", "task_id", "branch", "worktree", "authority_nonce", "receipt_sha256"}
        bridge = standard | {"kind", "proof_sha256"}
        valid = (
            (set(value) == standard and value.get("schema_version") == 1
             and _valid_authority_fields(value))
            or (set(value) == bridge and value.get("schema_version") == 2
                and value.get("kind") == "source-recovery-bridge"
                and _valid_authority_fields(value)
                and _valid_digest(value.get("proof_sha256")))
        )
    if not valid:
        raise GitPrivateStateError(f"invalid legacy private-state record: {path}")


def _namespace_kind(path: Path, *, foreign_regular: bool) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise GitPrivateStateError(f"cannot inspect private-state namespace: {path}") from exc
    if stat.S_ISREG(metadata.st_mode) and foreign_regular:
        return "foreign"
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        return "directory"
    raise GitPrivateStateError(f"unsafe private-state namespace: {path}")


def _scan_shared_legacy(layout: Topology) -> tuple[list[tuple[Path, Path]], Path | None]:
    root = layout.common / LEGACY_NAMESPACE
    kind = _namespace_kind(root, foreign_regular=True)
    if kind != "directory":
        return [], None
    pairs: list[tuple[Path, Path]] = []
    legacy_lock: Path | None = None
    for entry in root.iterdir():
        if entry.name == "cleanup.lock":
            _require_regular(entry, "legacy cleanup lock")
            legacy_lock = entry
            continue
        if entry.name not in SHARED_DIRS:
            raise GitPrivateStateError(f"unknown legacy private-state entry: {entry}")
        _require_dir(entry, "legacy private-state directory")
        for child in entry.iterdir():
            allow_fixed = entry.name == "automation-maintenance"
            if not _valid_shared_file(entry.name, child.name, allow_fixed=allow_fixed):
                raise GitPrivateStateError(f"unknown legacy private-state entry: {child}")
            _require_regular(child, "legacy private-state record")
            if child.name in FIXED_AUTHORITY_FILES:
                if layout.admin_is_common:
                    pairs.append((child, layout.admin / NAMESPACE / entry.name / child.name))
                # In a linked worktree these belong to the main worktree admin.
            else:
                pairs.append((child, layout.common / NAMESPACE / entry.name / child.name))
    return pairs, legacy_lock


def _scan_admin_legacy(layout: Topology) -> list[tuple[Path, Path]]:
    if layout.admin_is_common:
        return []
    root = layout.admin / LEGACY_NAMESPACE
    kind = _namespace_kind(root, foreign_regular=True)
    if kind != "directory":
        return []
    entries = list(root.iterdir())
    if any(entry.name != "automation-maintenance" for entry in entries):
        unknown = next(entry for entry in entries if entry.name != "automation-maintenance")
        raise GitPrivateStateError(f"unknown legacy private-state entry: {unknown}")
    if not entries:
        return []
    maintenance = entries[0]
    _require_dir(maintenance, "legacy maintenance authority directory")
    pairs: list[tuple[Path, Path]] = []
    for child in maintenance.iterdir():
        if child.name not in FIXED_AUTHORITY_FILES:
            raise GitPrivateStateError(f"unknown legacy private-state entry: {child}")
        _require_regular(child, "legacy private-state record")
        pairs.append((child, layout.admin / NAMESPACE / "automation-maintenance" / child.name))
    return pairs


def _validate_canonical(layout: Topology) -> None:
    shared = layout.common / NAMESPACE
    kind = _namespace_kind(shared, foreign_regular=False)
    if kind == "directory":
        for entry in shared.iterdir():
            if entry.name in LOCK_FILES:
                _require_regular(entry, "canonical private-state lock")
                continue
            if entry.name not in SHARED_DIRS:
                raise GitPrivateStateError(f"unknown canonical private-state entry: {entry}")
            _require_dir(entry)
            for child in entry.iterdir():
                # Fixed files in common are the main worktree's admin records.
                if not _valid_shared_file(entry.name, child.name, allow_fixed=True):
                    raise GitPrivateStateError(f"unknown canonical private-state entry: {child}")
                _require_regular(child, "canonical private-state record")
    if not layout.admin_is_common:
        admin_root = layout.admin / NAMESPACE
        kind = _namespace_kind(admin_root, foreign_regular=False)
        if kind == "directory":
            entries = list(admin_root.iterdir())
            if any(entry.name != "automation-maintenance" for entry in entries):
                unknown = next(entry for entry in entries if entry.name != "automation-maintenance")
                raise GitPrivateStateError(f"unknown canonical private-state entry: {unknown}")
            if entries:
                maintenance = entries[0]
                _require_dir(maintenance)
                for child in maintenance.iterdir():
                    if child.name not in FIXED_AUTHORITY_FILES:
                        raise GitPrivateStateError(f"unknown canonical private-state entry: {child}")
                    _require_regular(child, "canonical private-state record")


def _inspect_pairs(pairs: list[tuple[Path, Path]]) -> list[MigrationFile]:
    inspected: list[MigrationFile] = []
    for source, target in pairs:
        metadata = _require_regular(source, "legacy private-state record")
        content = read_bytes(source, "legacy private-state record")
        _validate_legacy_content(source, content)
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GitPrivateStateError(f"cannot inspect canonical private-state record: {target}") from exc
        else:
            if read_bytes(target, "canonical private-state record") != content:
                raise GitPrivateStateError(f"conflicting private-state records: {source} and {target}")
        inspected.append(MigrationFile(
            source, target, (metadata.st_dev, metadata.st_ino),
            stat.S_IMODE(metadata.st_mode), content,
        ))
    return inspected


def _exclusive_publish(path: Path, content: bytes, mode: int) -> tuple[int, int]:
    parent_fd = _open_anchored_parent(path)
    temporary = f".migrate.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short private-state write")
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
            os.fsync(parent_fd)
        except FileExistsError:
            if read_bytes(path, "canonical private-state record") != content:
                raise GitPrivateStateError(f"conflicting canonical private-state record: {path}")
        published = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode):
            raise GitPrivateStateError(f"unsafe canonical private-state record: {path}")
        return published.st_dev, published.st_ino
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot publish private-state record: {path}") from exc
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def ensure_parent(path: Path) -> None:
    parts = path.parts
    indexes = [index for index, part in enumerate(parts) if part == NAMESPACE]
    if not indexes:
        # Legacy restoration may use only an already-existing validated parent.
        descriptor = _open_anchored_parent(path)
        os.close(descriptor)
        return
    marker = indexes[-1]
    boundary = Path(*parts[:marker])
    _ensure_namespace(boundary, tuple(parts[marker + 1:-1]))


def exclusive_write_bytes(
    path: Path, content: bytes, mode: int = 0o600
) -> tuple[int, int]:
    """Durably publish a new state record without replacing any existing object."""
    ensure_parent(path)
    return _exclusive_publish(path, content, mode)


def _identity_unlink(item: MigrationFile) -> None:
    if read_bytes(item.source, "legacy private-state record") != item.content:
        raise GitPrivateStateError(f"legacy private-state record changed: {item.source}")
    metadata = _require_regular(item.source, "legacy private-state record")
    if (metadata.st_dev, metadata.st_ino) != item.identity:
        raise GitPrivateStateError(f"legacy private-state identity changed: {item.source}")
    parent_fd = _open_anchored_parent(item.source)
    try:
        current = os.stat(item.source.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != item.identity or not stat.S_ISREG(current.st_mode):
            raise GitPrivateStateError(f"legacy private-state identity changed: {item.source}")
        os.unlink(item.source.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot remove legacy private-state record: {item.source}") from exc
    finally:
        os.close(parent_fd)


def _remove_known_empty_legacy_directories(layout: Topology) -> None:
    roots = [layout.common / LEGACY_NAMESPACE]
    if not layout.admin_is_common:
        roots.append(layout.admin / LEGACY_NAMESPACE)
    for root in roots:
        if _namespace_kind(root, foreign_regular=True) != "directory":
            continue
        for name in SHARED_DIRS:
            child = root / name
            try:
                _require_dir(child, "legacy private-state directory")
            except GitPrivateStateError:
                if child.exists() or child.is_symlink():
                    raise
                continue
            if not any(child.iterdir()):
                child.rmdir()
                descriptor = _open_dir(root)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        # The shared root remains while its mixed-version cleanup lock exists.
        if not any(root.iterdir()):
            root.rmdir()
            descriptor = _open_dir(root.parent)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


@contextmanager
def _file_lock(path: Path, *, create: bool):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    parent_fd = _open_anchored_parent(path)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GitPrivateStateError(f"unsafe private-state lock: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot acquire private-state lock: {path}") from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(parent_fd)


def prepare(
    root: Path,
    *,
    admin: bool = False,
    common_dir: Path | None = None,
    admin_dir: Path | None = None,
    _legacy_lock_held: bool = False,
) -> None:
    """Validate and migrate known legacy state before a state mutation."""
    layout = (
        Topology(common_dir.resolve(), (admin_dir or common_dir).resolve())
        if common_dir is not None
        else topology(root)
    )
    _validate_canonical(layout)
    shared_pairs, legacy_lock = _scan_shared_legacy(layout)
    pairs = shared_pairs + (_scan_admin_legacy(layout) if admin else [])
    _inspect_pairs(pairs)  # preflight before creating even the migration lock
    if legacy_lock is not None and not _legacy_lock_held:
        with _file_lock(legacy_lock, create=False):
            prepare(
                root,
                admin=admin,
                common_dir=layout.common,
                admin_dir=layout.admin,
                _legacy_lock_held=True,
            )
        return

    _ensure_namespace(layout.common, ())
    with _file_lock(layout.common / NAMESPACE / "migration.lock", create=True):
        # Repeat all classification under the migration lock.
        _validate_canonical(layout)
        shared_pairs, _ = _scan_shared_legacy(layout)
        pairs = shared_pairs + (_scan_admin_legacy(layout) if admin else [])
        inspected = _inspect_pairs(pairs)
        for name in SHARED_DIRS:
            _ensure_namespace(layout.common, (name,))
        if admin:
            _ensure_namespace(layout.admin, ("automation-maintenance",))
        for item in inspected:
            try:
                item.target.lstat()
            except FileNotFoundError:
                _exclusive_publish(item.target, item.content, item.mode)
        # No source is removed until every destination is durable and equivalent.
        for item in inspected:
            if read_bytes(item.target, "canonical private-state record") != item.content:
                raise GitPrivateStateError(f"canonical private-state record changed: {item.target}")
        for item in inspected:
            metadata = _require_regular(item.source, "legacy private-state record")
            if (metadata.st_dev, metadata.st_ino) != item.identity or read_bytes(
                item.source, "legacy private-state record"
            ) != item.content:
                raise GitPrivateStateError(f"legacy private-state record changed: {item.source}")
        for item in inspected:
            _identity_unlink(item)
        _remove_known_empty_legacy_directories(layout)


def write_bytes(path: Path, content: bytes) -> None:
    """Atomically and durably replace one prepared canonical regular file."""
    parent_fd = _open_anchored_parent(path)
    temporary = f".record.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise GitPrivateStateError(f"unsafe canonical private-state record: {path}")
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short private-state write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot write private-state record: {path}") from exc
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def unlink(path: Path, *, expected_identity: tuple[int, int] | None = None) -> None:
    """Remove only a canonical regular file through its verified parent."""
    parent_fd = _open_anchored_parent(path)
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise GitPrivateStateError(f"unsafe canonical private-state record: {path}")
        if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise GitPrivateStateError(f"private-state record identity changed: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except GitPrivateStateError:
        raise
    except OSError as exc:
        raise GitPrivateStateError(f"cannot remove private-state record: {path}") from exc
    finally:
        os.close(parent_fd)


@contextmanager
def cleanup_lock(root: Path):
    """Acquire the retained legacy ABI lock first, then the canonical lock."""
    layout = topology(root)
    _validate_canonical(layout)
    _, legacy = _scan_shared_legacy(layout)
    # Lock old consumers out while migrating cleanup/discard evidence.
    legacy_context = _file_lock(legacy, create=False) if legacy is not None else nullcontext()
    with legacy_context:
        prepare(
            root,
            common_dir=layout.common,
            admin_dir=layout.admin,
            _legacy_lock_held=legacy is not None,
        )
        _ensure_namespace(layout.common, ())
        with _file_lock(layout.common / NAMESPACE / "cleanup.lock", create=True):
            yield
