from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"
sys.path.insert(0, str(BIN))
import git_private_state as private_state


def command(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class GitPrivateStateTest(unittest.TestCase):
    @staticmethod
    def valid_worktree(root: Path, suffix: str) -> str:
        path = root / ".worktrees" / suffix
        path.parent.mkdir(exist_ok=True)
        if (root / ".git").exists() and not path.exists():
            command("git", "worktree", "add", "-b", f"task/{suffix}", str(path), cwd=root)
        return str(path)

    @staticmethod
    def secure_canonical(path: Path, *, file: bool = False) -> None:
        cursor = path.parent if file else path
        while cursor.name != "agent-core":
            cursor.chmod(0o700)
            cursor = cursor.parent
        cursor.chmod(0o700)
        if file:
            path.chmod(0o600)
    def repository(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        command("git", "init", "-b", "main", cwd=repo)
        command("git", "config", "user.name", "Private State Test", cwd=repo)
        command("git", "config", "user.email", "private-state@example.invalid", cwd=repo)
        (repo / "seed").write_text("seed\n", encoding="utf-8")
        command("git", "add", "seed", cwd=repo)
        command("git", "commit", "-m", "seed", cwd=repo)
        return repo

    @staticmethod
    def identity(path: Path) -> tuple[int, int, int, int, int]:
        value = path.lstat()
        return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns

    def cleanup_record(self, root: Path, task: str = "1") -> dict:
        head = "a" * 40
        return {
            "schema_version": 1,
            "task": task,
            "status": "merged",
            "worktree": self.valid_worktree(root, f"{task}-test"),
            "branch": f"task/{task}-test",
            "local_head": head,
            "evidence": {
                "repository": "acme/widgets",
                "pr": 1,
                "published_head": head,
                "upstream": "deleted",
            },
        }

    def discard_record(self, root: Path, task: str = "1") -> dict:
        head = "b" * 40
        return {
            "schema_version": 1,
            "operation": "discard-pristine",
            "task": task,
            "status": "initialized",
            "worktree": self.valid_worktree(root, f"{task}-test"),
            "branch": f"task/{task}-test",
            "base_branch": "main",
            "base_revision": head,
            "local_head": head,
            "repository": "acme/widgets",
        }

    def authority_record(self, root: Path, task: str = "1") -> dict:
        return {
            "schema_version": 1,
            "task_id": task,
            "branch": f"task/{task}-test",
            "worktree": self.valid_worktree(root, f"{task}-test"),
            "authority_nonce": "9" * 64,
            "receipt_sha256": "a" * 64,
        }

    def proof_record(self, root: Path, task: str = "1") -> dict:
        authority = self.authority_record(root, task)
        return {
            "schema_version": 1,
            "kind": "source-recovery-proof",
            "task_id": task,
            "branch": authority["branch"],
            "worktree": authority["worktree"],
            "authority_head": "a" * 40,
            "authority_nonce": authority["authority_nonce"],
            "receipt_sha256": authority["receipt_sha256"],
            "receipt_bytes_sha256": "b" * 64,
            "changed_paths_sha256": "c" * 64,
            "path_fingerprints_sha256": "d" * 64,
            "implementation_source": "/tmp/source",
            "implementation_revision": "e" * 40,
            "receipt_source": "/tmp/source",
            "receipt_source_revision": "f" * 40,
        }

    def test_paths_are_pure_before_namespace_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            self.assertEqual(
                private_state.integration_checkpoint(repo, "12"),
                common / "agent-core" / "integration" / "pr-12.head",
            )
            self.assertFalse((common / "agent-core").exists())

    def test_regular_opencode_is_foreign_and_identity_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            foreign = private_state.common_git_dir(repo) / "opencode"
            foreign.write_bytes(b"0123456789abcdef0123456789abcdef01234567")
            before = self.identity(foreign)
            private_state.prepare(repo, admin=True)
            self.assertEqual(before, self.identity(foreign))
            self.assertEqual(b"0123456789abcdef0123456789abcdef01234567", foreign.read_bytes())

    def test_all_recognized_legacy_records_migrate_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy = common / "opencode"
            authority_worktree = Path(self.valid_worktree(repo, "TASK-1-test"))
            authority = {
                "schema_version": 1,
                "task_id": "TASK-1",
                "branch": "task/TASK-1-test",
                "worktree": str(authority_worktree),
                "authority_nonce": "9" * 64,
                "receipt_sha256": "a" * 64,
            }
            proof = {
                "schema_version": 1, "kind": "source-recovery-proof",
                "task_id": "TASK-1", "branch": "task/TASK-1-test",
                "worktree": str(authority_worktree), "authority_head": "a" * 40,
                "authority_nonce": "9" * 64, "receipt_sha256": "a" * 64,
                "receipt_bytes_sha256": "b" * 64, "changed_paths_sha256": "c" * 64,
                "path_fingerprints_sha256": "d" * 64,
                "implementation_source": "/tmp/source", "implementation_revision": "e" * 40,
                "receipt_source": "/tmp/source", "receipt_source_revision": "f" * 40,
            }
            bridge = {
                "schema_version": 2, "kind": "source-recovery-bridge",
                "task_id": authority["task_id"], "branch": authority["branch"],
                "worktree": authority["worktree"],
                "authority_nonce": authority["authority_nonce"],
                "receipt_sha256": authority["receipt_sha256"],
                "proof_sha256": hashlib.sha256(
                    json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            records = {
                "cleanup/TASK-1.json": json.dumps({
                    "schema_version": 1, "task": "TASK-1", "status": "merged",
                    "worktree": self.valid_worktree(Path(directory), "TASK-1-test"), "branch": "task/TASK-1-test",
                    "local_head": "a" * 40, "evidence": {"repository": "acme/widgets", "pr": 12, "published_head": "a" * 40, "upstream": "deleted"},
                }).encode(),
                "integration/pr-12.head": b"a" * 40 + b"\n",
                "discard-pristine/13.json": json.dumps({
                    "schema_version": 1, "operation": "discard-pristine", "task": "13",
                    "status": "initialized", "worktree": self.valid_worktree(Path(directory), "13-test"),
                    "branch": "task/13-test", "base_branch": "main",
                    "base_revision": "b" * 40, "local_head": "b" * 40,
                    "repository": "acme/widgets",
                }).encode(),
                f"automation-maintenance/{hashlib.sha256(str(Path(authority['worktree']).resolve()).encode()).hexdigest()}.json": json.dumps(authority).encode(),
                "automation-maintenance/authority.json": json.dumps(bridge).encode(),
                "automation-maintenance/source-recovery-proof.json": json.dumps(proof).encode(),
            }
            for relative, content in records.items():
                path = legacy / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            legacy_lock = legacy / "cleanup.lock"
            legacy_lock.write_bytes(b"")
            legacy_lock_identity = self.identity(legacy_lock)

            private_state.prepare(authority_worktree, admin=True)

            for relative, content in records.items():
                base = (
                    private_state.admin_git_dir(authority_worktree) / "agent-core"
                    if Path(relative).name in private_state.FIXED_AUTHORITY_FILES
                    else common / "agent-core"
                )
                self.assertEqual(content, (base / relative).read_bytes())
                self.assertFalse((legacy / relative).exists())
            self.assertFalse(legacy.exists())
            canonical_lock = common / "agent-core/cleanup.lock"
            self.assertEqual(legacy_lock_identity[0:2], self.identity(canonical_lock)[0:2])
            self.assertEqual(0o600, stat.S_IMODE(canonical_lock.stat().st_mode))
            private_state.prepare(authority_worktree, admin=True)

    def test_contended_legacy_cleanup_lock_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy_lock = common / "opencode/cleanup.lock"
            legacy_lock.parent.mkdir()
            legacy_lock.write_bytes(b"")
            with legacy_lock.open("r+b") as handle:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "BLOCKED.*contended"):
                    private_state.prepare(repo)
            self.assertTrue(legacy_lock.is_file())
            self.assertFalse((common / "agent-core").exists())

    def test_cleanup_operation_uses_handed_off_inode_without_relocking_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy_lock = common / "opencode/cleanup.lock"
            legacy_lock.parent.mkdir()
            legacy_lock.write_bytes(b"")
            inode = legacy_lock.stat().st_ino
            with private_state.cleanup_lock(repo):
                self.assertFalse((common / "opencode").exists())
                self.assertEqual(inode, (common / "agent-core/cleanup.lock").stat().st_ino)

    def test_cleanup_handoff_rejects_legacy_path_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy_lock = common / "opencode/cleanup.lock"
            legacy_lock.parent.mkdir()
            legacy_lock.write_bytes(b"")
            original = private_state._handoff_cleanup_lock

            def replace_before_handoff(legacy, canonical, expected_identity):
                legacy.unlink()
                legacy.write_bytes(b"replacement")
                return original(legacy, canonical, expected_identity)

            with mock.patch.object(
                private_state, "_handoff_cleanup_lock", side_effect=replace_before_handoff
            ):
                with self.assertRaisesRegex(
                    private_state.GitPrivateStateError, "identity changed"
                ):
                    with private_state.cleanup_lock(repo):
                        self.fail("unsafe cleanup lock handoff succeeded")
            self.assertFalse((common / "agent-core/cleanup.lock").exists())
            self.assertEqual(b"replacement", legacy_lock.read_bytes())

    def test_cleanup_handoff_preserves_unknown_canonical_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy_lock = common / "opencode/cleanup.lock"
            canonical_lock = common / "agent-core/cleanup.lock"
            legacy_lock.parent.mkdir()
            legacy_lock.write_bytes(b"")
            real_stat = os.stat
            replaced = False

            def replace_canonical_before_postlink_stat(path, *args, **kwargs):
                nonlocal replaced
                if (
                    not replaced
                    and path == canonical_lock.name
                    and kwargs.get("dir_fd") is not None
                    and os.path.lexists(canonical_lock)
                ):
                    replaced = True
                    canonical_lock.unlink()
                    canonical_lock.write_bytes(b"unknown")
                    canonical_lock.chmod(0o600)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(os, "stat", side_effect=replace_canonical_before_postlink_stat):
                with self.assertRaisesRegex(
                    private_state.GitPrivateStateError, "identity changed during handoff"
                ):
                    with private_state.cleanup_lock(repo):
                        self.fail("unsafe cleanup lock handoff succeeded")
            self.assertTrue(replaced)
            self.assertEqual(b"unknown", canonical_lock.read_bytes())

    def test_distinct_dual_cleanup_locks_block_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy = common / "opencode/cleanup.lock"
            legacy.parent.mkdir()
            legacy.write_bytes(b"")
            canonical = common / "agent-core/cleanup.lock"
            canonical.parent.mkdir()
            canonical.parent.chmod(0o700)
            canonical.write_bytes(b"")
            canonical.chmod(0o600)
            legacy_before = self.identity(legacy)
            canonical_before = self.identity(canonical)
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "BLOCKED.*different inodes"):
                private_state.prepare(repo)
            self.assertEqual(legacy_before, self.identity(legacy))
            self.assertEqual(canonical_before, self.identity(canonical))
            self.assertFalse((common / "agent-core/migration.lock").exists())

    def test_conflict_fails_before_any_migration_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            first = common / "opencode/cleanup/1.json"
            conflict = common / "opencode/integration/pr-2.head"
            destination = common / "agent-core/integration/pr-2.head"
            first.parent.mkdir(parents=True)
            conflict.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            self.secure_canonical(destination.parent)
            first.write_bytes(json.dumps({
                "schema_version": 1, "task": "1", "status": "merged",
                "worktree": self.valid_worktree(repo, "1-test"), "branch": "task/1-test",
                "local_head": "a" * 40, "evidence": {"repository": "acme/widgets", "pr": 1, "published_head": "a" * 40, "upstream": "deleted"},
            }).encode())
            conflict.write_bytes(b"a" * 40 + b"\n")
            destination.write_bytes(b"b" * 40 + b"\n")
            destination.chmod(0o600)
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "conflicting"):
                private_state.prepare(repo)
            self.assertTrue(first.is_file())
            self.assertEqual(b"a" * 40 + b"\n", conflict.read_bytes())
            self.assertEqual(b"b" * 40 + b"\n", destination.read_bytes())
            self.assertFalse((common / "agent-core/migration.lock").exists())

    def test_unknown_legacy_entry_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            unknown = common / "opencode/unexpected"
            unknown.mkdir(parents=True)
            (unknown / "data").write_bytes(b"x")
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "unknown legacy"):
                private_state.prepare(repo)
            self.assertFalse((common / "agent-core").exists())
            self.assertEqual(b"x", (unknown / "data").read_bytes())

    def test_matching_legacy_name_with_invalid_schema_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            foreign = common / "opencode/cleanup/1.json"
            foreign.parent.mkdir(parents=True)
            foreign.write_bytes(b"arbitrary foreign bytes")
            before = self.identity(foreign)
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "invalid legacy"):
                private_state.prepare(repo)
            self.assertEqual(before, self.identity(foreign))
            self.assertEqual(b"arbitrary foreign bytes", foreign.read_bytes())
            self.assertFalse((common / "agent-core").exists())

    def test_schema_shaped_invalid_authority_and_proof_are_not_claimed(self) -> None:
        authority = {
            "schema_version": 1, "task_id": "TASK-1", "branch": "task/TASK-1-test",
            "worktree": "/tmp/worktree", "authority_nonce": "9" * 64,
            "receipt_sha256": "a" * 64,
        }
        proof = {
            "schema_version": 1, "kind": "source-recovery-proof",
            "task_id": "TASK-1", "branch": "task/TASK-1-test",
            "worktree": "/tmp/worktree", "authority_head": "a" * 40,
            "authority_nonce": "9" * 64, "receipt_sha256": "a" * 64,
            "receipt_bytes_sha256": "b" * 64, "changed_paths_sha256": "c" * 64,
            "path_fingerprints_sha256": "d" * 64,
            "implementation_source": "/tmp/source", "implementation_revision": "e" * 40,
            "receipt_source": "/tmp/source", "receipt_source_revision": "f" * 40,
        }
        cases = (
            ("authority.json", dict(authority, authority_nonce="not-a-nonce")),
            ("authority.json", dict(authority, worktree="relative/worktree")),
            ("source-recovery-proof.json", dict(proof, authority_head="not-an-oid")),
            ("source-recovery-proof.json", dict(proof, authority_head="a" * 41)),
            ("source-recovery-proof.json", dict(proof, implementation_revision="e" * 63)),
            ("source-recovery-proof.json", dict(proof, receipt_bytes_sha256="not-a-digest")),
            ("source-recovery-proof.json", dict(proof, implementation_source="relative/source")),
            ("source-recovery-proof.json", dict(proof, receipt_source_revision="not-an-oid")),
        )
        for name, record in cases:
            with self.subTest(name=name, record=record), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                foreign = common / "opencode/automation-maintenance" / name
                foreign.parent.mkdir(parents=True)
                foreign.write_text(json.dumps(record), encoding="utf-8")
                before = self.identity(foreign)
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "invalid legacy"):
                    private_state.prepare(repo, admin=True)
                self.assertEqual(before, self.identity(foreign))
                self.assertEqual(record, json.loads(foreign.read_text(encoding="utf-8")))
                self.assertFalse((common / "agent-core").exists())

    def test_malformed_integration_oid_is_not_claimed(self) -> None:
        for content in (b"a" * 41 + b"\n", b"a" * 63 + b"\n", b"A" * 40 + b"\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                foreign = common / "opencode/integration/pr-1.head"
                foreign.parent.mkdir(parents=True)
                foreign.write_bytes(content)
                before = self.identity(foreign)
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "invalid legacy"):
                    private_state.prepare(repo)
                self.assertEqual(before, self.identity(foreign))
                self.assertEqual(content, foreign.read_bytes())
                self.assertFalse((common / "agent-core").exists())

    def test_legacy_symlink_and_special_file_fail_closed(self) -> None:
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                legacy = common / "opencode/cleanup"
                legacy.mkdir(parents=True)
                unsafe = legacy / "1.json"
                if kind == "symlink":
                    outside = Path(directory) / "outside"
                    outside.write_bytes(b"outside")
                    unsafe.symlink_to(outside)
                else:
                    os.mkfifo(unsafe)
                with self.assertRaises(private_state.GitPrivateStateError):
                    private_state.prepare(repo)
                self.assertFalse((common / "agent-core").exists())

    def test_equivalent_dual_state_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            legacy = common / "opencode/cleanup/7.json"
            current = common / "agent-core/cleanup/7.json"
            legacy.parent.mkdir(parents=True)
            current.parent.mkdir(parents=True)
            self.secure_canonical(current.parent)
            content = json.dumps({
                "schema_version": 1, "task": "7", "status": "merged",
                "worktree": self.valid_worktree(repo, "7-test"), "branch": "task/7-test",
                "local_head": "a" * 40, "evidence": {"repository": "acme/widgets", "pr": 7, "published_head": "a" * 40, "upstream": "deleted"},
            }).encode()
            legacy.write_bytes(content)
            current.write_bytes(content)
            current.chmod(0o600)
            private_state.prepare(repo)
            self.assertFalse(legacy.exists())
            self.assertEqual(content, current.read_bytes())
            private_state.prepare(repo)

    def test_owned_crash_temporaries_are_recovered_and_migration_resumes(self) -> None:
        for temporary_content in (b"partial", json.dumps(self.cleanup_record(Path("/tmp"))).encode()):
            with self.subTest(size=len(temporary_content)), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                record = self.cleanup_record(repo)
                content = json.dumps(record).encode()
                source = common / "opencode/cleanup/1.json"
                source.parent.mkdir(parents=True)
                source.write_bytes(content)
                temporary = common / "agent-core/cleanup/.migrate.123.0123456789abcdef"
                temporary.parent.mkdir(parents=True)
                self.secure_canonical(temporary.parent)
                temporary.write_bytes(temporary_content)
                temporary.chmod(0o600)

                private_state.prepare(repo)

                self.assertFalse(temporary.exists())
                self.assertFalse((common / "opencode").exists())
                self.assertEqual(content, (common / "agent-core/cleanup/1.json").read_bytes())

    def test_durable_destination_with_leftover_temp_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            destination = common / "agent-core/cleanup/1.json"
            destination.parent.mkdir(parents=True)
            self.secure_canonical(destination.parent)
            destination.write_text(json.dumps(self.cleanup_record(repo)), encoding="utf-8")
            destination.chmod(0o600)
            temporary = destination.parent / ".record.123.0123456789abcdef"
            temporary.write_bytes(b"complete but stale")
            temporary.chmod(0o600)

            private_state.prepare(repo)

            self.assertFalse(temporary.exists())
            self.assertTrue(destination.is_file())

    def test_partial_legacy_removal_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            canonical = common / "agent-core/cleanup/1.json"
            canonical.parent.mkdir(parents=True)
            self.secure_canonical(canonical.parent)
            first = json.dumps(self.cleanup_record(repo, "1")).encode()
            second = json.dumps(self.cleanup_record(repo, "2")).encode()
            canonical.write_bytes(first)
            canonical.chmod(0o600)
            remaining = common / "opencode/cleanup/2.json"
            remaining.parent.mkdir(parents=True)
            remaining.write_bytes(second)

            private_state.prepare(repo)

            self.assertEqual(first, canonical.read_bytes())
            self.assertEqual(second, (canonical.parent / "2.json").read_bytes())
            self.assertFalse((common / "opencode").exists())

    def test_malformed_or_unsafe_canonical_temp_fails_closed(self) -> None:
        for name, mode in ((".migrate.bad", 0o600), (".record.1.0123456789abcdef", 0o666)):
            with self.subTest(name=name, mode=mode), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                temporary = common / "agent-core/cleanup" / name
                temporary.parent.mkdir(parents=True)
                self.secure_canonical(temporary.parent)
                temporary.write_bytes(b"stale")
                temporary.chmod(mode)
                before = self.identity(temporary)
                with self.assertRaises(private_state.GitPrivateStateError):
                    private_state.prepare(repo)
                self.assertEqual(before, self.identity(temporary))

    def test_unsafe_legacy_modes_are_not_promoted(self) -> None:
        cases = ("receipt", "authority", "namespace", "subdirectory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                if case == "authority":
                    value = self.authority_record(repo)
                    key = hashlib.sha256(str(Path(value["worktree"]).resolve()).encode()).hexdigest()
                    source = common / "opencode/automation-maintenance" / f"{key}.json"
                else:
                    value = self.cleanup_record(repo)
                    source = common / "opencode/cleanup/1.json"
                source.parent.mkdir(parents=True)
                source.write_text(json.dumps(value), encoding="utf-8")
                unsafe = source
                if case == "namespace":
                    unsafe = common / "opencode"
                elif case == "subdirectory":
                    unsafe = source.parent
                unsafe.chmod(0o777 if unsafe.is_dir() else 0o666)
                before = self.identity(source)
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "unsafe"):
                    private_state.prepare(repo)
                self.assertEqual(before, self.identity(source))
                self.assertFalse((common / "agent-core").exists())

    def test_unsafe_canonical_directory_or_record_fails_closed(self) -> None:
        for case in ("directory", "record"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                common = private_state.common_git_dir(repo)
                record = common / "agent-core/cleanup/1.json"
                record.parent.mkdir(parents=True)
                self.secure_canonical(record.parent)
                record.write_text(json.dumps(self.cleanup_record(repo)), encoding="utf-8")
                record.chmod(0o600)
                (record.parent if case == "directory" else record).chmod(0o777 if case == "directory" else 0o666)
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "unsafe"):
                    private_state.prepare(repo)

    def test_historical_semantic_violations_are_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = self.cleanup_record(root)
            discard = self.discard_record(root)
            cases = (
                ("cleanup/1.json", dict(cleanup, branch="task/2-foreign")),
                ("cleanup/1.json", dict(cleanup, evidence={"repository": "acme/widgets"})),
                ("discard-pristine/1.json", dict(discard, branch="fix/2-foreign")),
                ("discard-pristine/1.json", dict(discard, worktree="/tmp/foreign")),
                ("discard-pristine/1.json", dict(discard, base_branch="")),
                ("discard-pristine/1.json", dict(discard, repository="invalid")),
            )
            for relative, record in cases:
                with self.subTest(relative=relative, record=record), tempfile.TemporaryDirectory() as repo_dir:
                    repo = self.repository(Path(repo_dir))
                    common = private_state.common_git_dir(repo)
                    source = common / "opencode" / relative
                    source.parent.mkdir(parents=True)
                    raw = json.dumps(record).encode()
                    source.write_bytes(raw)
                    before = self.identity(source)
                    with self.assertRaisesRegex(private_state.GitPrivateStateError, "invalid legacy"):
                        private_state.prepare(repo)
                    self.assertEqual(before, self.identity(source))
                    self.assertEqual(raw, source.read_bytes())
                    self.assertFalse((common / "agent-core").exists())

    def test_wrong_hashed_authority_filename_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            source = common / "opencode/automation-maintenance" / f"{'0' * 64}.json"
            source.parent.mkdir(parents=True)
            raw = json.dumps(self.authority_record(repo)).encode()
            source.write_bytes(raw)
            before = self.identity(source)
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "filename"):
                private_state.prepare(repo)
            self.assertEqual(before, self.identity(source))
            self.assertFalse((common / "agent-core").exists())

    def test_mismatched_legacy_proof_and_bridge_are_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            proof = self.proof_record(repo, "1")
            other = self.authority_record(repo, "2")
            bridge = {
                **other,
                "schema_version": 2,
                "kind": "source-recovery-bridge",
                "proof_sha256": hashlib.sha256(
                    json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            maintenance = common / "opencode/automation-maintenance"
            maintenance.mkdir(parents=True)
            proof_path = maintenance / "source-recovery-proof.json"
            authority_path = maintenance / "authority.json"
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            authority_path.write_text(json.dumps(bridge), encoding="utf-8")
            before = (self.identity(proof_path), self.identity(authority_path))
            with self.assertRaisesRegex(
                private_state.GitPrivateStateError,
                "conflicting legacy|different worktree admin",
            ):
                private_state.prepare(repo, admin=True)
            self.assertEqual(before, (self.identity(proof_path), self.identity(authority_path)))
            self.assertFalse((common / "agent-core").exists())

    def test_legacy_bridge_without_proof_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            authority = self.authority_record(repo)
            bridge = {
                **authority,
                "schema_version": 2,
                "kind": "source-recovery-bridge",
                "proof_sha256": "f" * 64,
            }
            source = common / "opencode/automation-maintenance/authority.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(bridge), encoding="utf-8")
            before = self.identity(source)
            linked = Path(bridge["worktree"])
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "lacks its proof"):
                private_state.prepare(linked, admin=True)
            self.assertEqual(before, self.identity(source))
            self.assertFalse((common / "agent-core").exists())

    def test_legacy_bridge_recovers_after_proof_was_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            proof = self.proof_record(repo)
            linked = Path(proof["worktree"])
            bridge = {
                **self.authority_record(repo),
                "schema_version": 2,
                "kind": "source-recovery-bridge",
                "proof_sha256": hashlib.sha256(
                    json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            legacy = common / "opencode/automation-maintenance"
            canonical = (
                private_state.admin_git_dir(linked)
                / "agent-core/automation-maintenance"
            )
            legacy.mkdir(parents=True)
            canonical.mkdir(parents=True)
            canonical.parent.chmod(0o700)
            canonical.chmod(0o700)
            (legacy / "authority.json").write_text(json.dumps(bridge), encoding="utf-8")
            (canonical / "source-recovery-proof.json").write_text(
                json.dumps(proof), encoding="utf-8"
            )
            (canonical / "source-recovery-proof.json").chmod(0o600)
            private_state.prepare(linked, admin=True)
            self.assertFalse((common / "opencode").exists())
            self.assertTrue((canonical / "authority.json").is_file())

    def test_unrelated_absolute_authority_worktree_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            authority = self.authority_record(repo)
            authority["worktree"] = str(Path(directory) / "unrelated")
            source = common / "opencode/automation-maintenance/authority.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(authority), encoding="utf-8")
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "not registered"):
                private_state.prepare(repo, admin=True)
            self.assertTrue(source.is_file())

    def test_incomplete_or_nonbridge_canonical_recovery_pair_is_rejected(self) -> None:
        for case in ("proof-only", "bridge-only", "standard-with-proof"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo = self.repository(Path(directory))
                proof = self.proof_record(repo)
                linked = Path(proof["worktree"])
                authority = self.authority_record(repo)
                bridge = {
                    **authority,
                    "schema_version": 2,
                    "kind": "source-recovery-bridge",
                    "proof_sha256": hashlib.sha256(
                        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
                maintenance = (
                    private_state.admin_git_dir(linked)
                    / "agent-core/automation-maintenance"
                )
                maintenance.mkdir(parents=True)
                maintenance.parent.chmod(0o700)
                maintenance.chmod(0o700)
                records = (
                    {"source-recovery-proof.json": proof}
                    if case == "proof-only"
                    else {"authority.json": bridge}
                    if case == "bridge-only"
                    else {
                        "authority.json": authority,
                        "source-recovery-proof.json": proof,
                    }
                )
                for name, value in records.items():
                    record = maintenance / name
                    record.write_text(json.dumps(value), encoding="utf-8")
                    record.chmod(0o600)
                with self.assertRaises(private_state.GitPrivateStateError):
                    private_state.prepare(linked, admin=True)

    def test_legacy_unlink_consumes_equivalent_migrated_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            content = (json.dumps(self.authority_record(repo)) + "\n").encode()
            value = json.loads(content)
            name = hashlib.sha256(
                str(Path(value["worktree"]).resolve()).encode()
            ).hexdigest() + ".json"
            legacy = common / "opencode/automation-maintenance" / name
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(content)
            identity = self.identity(legacy)[:2]
            private_state.prepare(repo, admin=True)
            canonical = common / "agent-core/automation-maintenance" / name
            self.assertTrue(canonical.is_file())
            private_state.unlink(
                legacy, expected_identity=identity, expected_content=content
            )
            self.assertFalse(canonical.exists())

    def test_legacy_unlink_consumes_equivalent_dual_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            content = (json.dumps(self.cleanup_record(Path(directory))) + "\n").encode()
            legacy = common / "opencode/cleanup/1.json"
            canonical = common / "agent-core/cleanup/1.json"
            legacy.parent.mkdir(parents=True)
            canonical.parent.mkdir(parents=True)
            canonical.parent.parent.chmod(0o700)
            canonical.parent.chmod(0o700)
            legacy.write_bytes(content)
            canonical.write_bytes(content)
            canonical.chmod(0o600)
            identity = self.identity(legacy)[:2]
            private_state.unlink(
                legacy, expected_identity=identity, expected_content=content
            )
            self.assertFalse(legacy.exists())
            self.assertFalse(canonical.exists())

    def test_unsafe_matching_target_race_does_not_consume_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            source = common / "opencode/cleanup/1.json"
            source.parent.mkdir(parents=True)
            raw = json.dumps(self.cleanup_record(repo)).encode()
            source.write_bytes(raw)
            before = self.identity(source)
            target = common / "agent-core/cleanup/1.json"
            original = private_state._exclusive_publish

            def inject_unsafe(path: Path, content: bytes, mode: int):
                if path == target and not path.exists():
                    path.write_bytes(content)
                    path.chmod(0o666)
                return original(path, content, mode)

            with mock.patch.object(private_state, "_exclusive_publish", side_effect=inject_unsafe):
                with self.assertRaisesRegex(private_state.GitPrivateStateError, "unsafe canonical"):
                    private_state.prepare(repo)
            self.assertEqual(before, self.identity(source))
            self.assertEqual(raw, source.read_bytes())

    def test_namespace_symlink_cannot_redirect_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (common / "agent-core").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(private_state.GitPrivateStateError):
                private_state.prepare(repo)
            self.assertEqual([], list(outside.iterdir()))

    def test_parent_replacement_after_open_cannot_redirect_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            private_state.prepare(repo)
            namespace = common / "agent-core"
            saved = common / "agent-core-saved"
            outside = Path(directory) / "outside"
            outside.mkdir()
            target = private_state.integration_checkpoint(repo, "8")
            real_replace = private_state.os.replace
            replaced = False

            def swap_then_replace(*args, **kwargs):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    namespace.rename(saved)
                    namespace.symlink_to(outside, target_is_directory=True)
                return real_replace(*args, **kwargs)

            with mock.patch.object(private_state.os, "replace", side_effect=swap_then_replace):
                private_state.write_bytes(target, b"a" * 40 + b"\n")
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual(b"a" * 40 + b"\n", (saved / "integration/pr-8.head").read_bytes())

    def test_opencode_symlink_cannot_redirect_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            common = private_state.common_git_dir(repo)
            outside = Path(directory) / "outside"
            (outside / "cleanup").mkdir(parents=True)
            (outside / "cleanup/1.json").write_bytes(b"outside")
            (common / "opencode").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(private_state.GitPrivateStateError):
                private_state.prepare(repo)
            self.assertEqual(b"outside", (outside / "cleanup/1.json").read_bytes())
            self.assertFalse((common / "agent-core").exists())

    def test_linked_worktree_routes_fixed_authority_to_its_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            linked = repo / ".worktrees" / "1-test"
            command("git", "worktree", "add", "-b", "task/1-test", str(linked), cwd=repo)
            common = private_state.common_git_dir(linked)
            authority = common / "opencode/automation-maintenance/authority.json"
            authority.parent.mkdir(parents=True)
            content = json.dumps({
                "schema_version": 1, "task_id": "1", "branch": "task/1-test",
                "worktree": str(linked), "authority_nonce": "a" * 64,
                "receipt_sha256": "a" * 64,
            }).encode()
            authority.write_bytes(content)
            before = self.identity(authority)
            private_state.prepare(linked, admin=True)
            self.assertFalse(authority.exists())
            migrated = private_state.admin_git_dir(linked) / "agent-core/automation-maintenance/authority.json"
            self.assertEqual(content, migrated.read_bytes())
            self.assertNotEqual(before, self.identity(migrated))
            self.assertTrue((private_state.admin_git_dir(linked) / "agent-core/automation-maintenance").is_dir())

    def test_common_prepare_does_not_recover_unlocked_admin_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            linked = repo / ".worktrees" / "1-test"
            command("git", "worktree", "add", "-b", "task/1-test", str(linked), cwd=repo)
            admin = private_state.admin_git_dir(linked)
            maintenance = admin / "agent-core/automation-maintenance"
            maintenance.mkdir(parents=True)
            (admin / "agent-core").chmod(0o700)
            maintenance.chmod(0o700)
            temporary = maintenance / (".record.1." + "a" * 16)
            temporary.write_bytes(b"active")
            temporary.chmod(0o600)
            private_state.prepare(linked, admin=False)
            self.assertEqual(b"active", temporary.read_bytes())


if __name__ == "__main__":
    unittest.main()
