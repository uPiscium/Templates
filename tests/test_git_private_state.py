from __future__ import annotations

import json
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
            authority = {
                "schema_version": 1,
                "task_id": "TASK-1",
                "branch": "task/TASK-1-test",
                "worktree": "/tmp/worktree",
                "authority_nonce": "9" * 64,
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
            records = {
                "cleanup/TASK-1.json": json.dumps({
                    "schema_version": 1, "task": "TASK-1", "status": "merged",
                    "worktree": "/tmp/worktree", "branch": "task/TASK-1-test",
                    "local_head": "a" * 40, "evidence": {},
                }).encode(),
                "integration/pr-12.head": b"a" * 40 + b"\n",
                "discard-pristine/13.json": json.dumps({
                    "schema_version": 1, "operation": "discard-pristine", "task": "13",
                    "status": "initialized", "worktree": "/tmp/worktree",
                    "branch": "task/13-test", "base_branch": "main",
                    "base_revision": "b" * 40, "local_head": "b" * 40,
                    "repository": "acme/widgets",
                }).encode(),
                f"automation-maintenance/{'b' * 64}.json": json.dumps(authority).encode(),
                "automation-maintenance/authority.json": json.dumps(authority).encode(),
                "automation-maintenance/source-recovery-proof.json": json.dumps(proof).encode(),
            }
            for relative, content in records.items():
                path = legacy / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (legacy / "cleanup.lock").write_bytes(b"")

            private_state.prepare(repo, admin=True)

            for relative, content in records.items():
                self.assertEqual(content, (common / "agent-core" / relative).read_bytes())
                self.assertFalse((legacy / relative).exists())
            self.assertTrue((legacy / "cleanup.lock").is_file())
            private_state.prepare(repo, admin=True)

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
            first.write_bytes(json.dumps({
                "schema_version": 1, "task": "1", "status": "merged",
                "worktree": "/tmp/worktree", "branch": "task/1-test",
                "local_head": "a" * 40, "evidence": {},
            }).encode())
            conflict.write_bytes(b"a" * 40 + b"\n")
            destination.write_bytes(b"new")
            with self.assertRaisesRegex(private_state.GitPrivateStateError, "conflicting"):
                private_state.prepare(repo)
            self.assertTrue(first.is_file())
            self.assertEqual(b"a" * 40 + b"\n", conflict.read_bytes())
            self.assertEqual(b"new", destination.read_bytes())
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
            content = json.dumps({
                "schema_version": 1, "task": "7", "status": "merged",
                "worktree": "/tmp/worktree", "branch": "task/7-test",
                "local_head": "a" * 40, "evidence": {},
            }).encode()
            legacy.write_bytes(content)
            current.write_bytes(content)
            private_state.prepare(repo)
            self.assertFalse(legacy.exists())
            self.assertEqual(content, current.read_bytes())
            private_state.prepare(repo)

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

    def test_linked_worktree_preserves_main_admin_fixed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repository(Path(directory))
            linked = Path(directory) / "linked"
            command("git", "worktree", "add", "-b", "task/1-test", str(linked), cwd=repo)
            common = private_state.common_git_dir(linked)
            authority = common / "opencode/automation-maintenance/authority.json"
            authority.parent.mkdir(parents=True)
            content = json.dumps({
                "schema_version": 1, "task_id": "1", "branch": "task/1-test",
                "worktree": str(repo), "authority_nonce": "nonce",
                "receipt_sha256": "a" * 64,
            }).encode()
            authority.write_bytes(content)
            before = self.identity(authority)
            private_state.prepare(linked, admin=True)
            self.assertEqual(before, self.identity(authority))
            self.assertEqual(content, authority.read_bytes())
            self.assertTrue((private_state.admin_git_dir(linked) / "agent-core/automation-maintenance").is_dir())


if __name__ == "__main__":
    unittest.main()
