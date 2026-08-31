from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "source_finalize_bridge_test", ROOT / "tools" / "automation_recovery_bridge.py"
)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
bridge = importlib.util.module_from_spec(BRIDGE_SPEC)
BRIDGE_SPEC.loader.exec_module(bridge)

MAINTENANCE_SPEC = importlib.util.spec_from_file_location(
    "source_finalize_maintenance_test", BIN / "maintenance_lifecycle.py"
)
assert MAINTENANCE_SPEC and MAINTENANCE_SPEC.loader
maintenance = importlib.util.module_from_spec(MAINTENANCE_SPEC)
MAINTENANCE_SPEC.loader.exec_module(maintenance)


class FirstAdoptionFinalizeBridgeTest(unittest.TestCase):
    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ("git", *args), cwd=cwd, check=True, text=True, capture_output=True
        )
        return result.stdout.strip()

    def configure(self, repo: Path) -> None:
        self.git(repo, "config", "user.name", "Bootstrap Finalize Test")
        self.git(repo, "config", "user.email", "bootstrap@example.invalid")

    def test_stale_main_bootstraps_fast_forward_and_exact_idempotent_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            remote = top / "origin.git"
            main = top / "consumer"
            publisher = top / "publisher"
            subprocess.run(
                ("git", "init", "--bare", "--initial-branch=main", str(remote)),
                cwd=top,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ("git", "init", "--initial-branch=main", str(main)),
                cwd=top,
                check=True,
                capture_output=True,
                text=True,
            )
            self.configure(main)
            (main / "Justfile").write_text("default:\n    @just --list\n", encoding="utf-8")
            self.git(main, "add", "Justfile")
            self.git(main, "commit", "-m", "old Agent Core")
            stale_main = self.git(main, "rev-parse", "HEAD")
            self.git(main, "remote", "add", "origin", str(remote))
            self.git(main, "push", "-u", "origin", "main")
            self.git(
                main,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            )
            common = Path(self.git(main, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = main / common
            (common / "info").mkdir(parents=True, exist_ok=True)
            (common / "info" / "exclude").write_text(
                "/.worktrees/\n/.task-state/\n", encoding="utf-8"
            )

            task = main / ".worktrees" / "22-maintenance"
            self.git(main, "worktree", "add", "-b", "task/22-maintenance", str(task))
            (task / "Justfile").write_text(
                "maintenance-finalize task pr:\n    @true\n", encoding="utf-8"
            )
            self.git(task, "add", "Justfile")
            self.git(task, "commit", "-m", "install current Agent Core")
            task_head = self.git(task, "rev-parse", "HEAD")
            self.git(task, "push", "-u", "origin", "task/22-maintenance")

            state_dir = task / ".task-state"
            state_dir.mkdir()
            state = state_dir / "task.md"
            state.write_text(
                "# Task 22\n\n## Identity\n\n"
                "- Task ID: 22\n- Branch: task/22-maintenance\n"
                f"- Worktree: {task}\n\n## Current state\n\n"
                "- Status: initialized\n- Blockers: none\n- Unverified: none\n",
                encoding="utf-8",
            )

            subprocess.run(
                ("git", "clone", str(remote), str(publisher)),
                cwd=top,
                check=True,
                capture_output=True,
                text=True,
            )
            self.configure(publisher)
            self.git(publisher, "fetch", "origin", "task/22-maintenance")
            self.git(publisher, "merge", "--no-ff", "--no-edit", "origin/task/22-maintenance")
            merge_oid = self.git(publisher, "rev-parse", "HEAD")
            self.git(publisher, "push", "origin", "main")
            self.assertEqual(self.git(main, "rev-parse", "HEAD"), stale_main)
            self.assertNotIn("maintenance-finalize", (main / "Justfile").read_text(encoding="utf-8"))

            record = maintenance.lifecycle.WorktreeRecord(
                task, "task/22-maintenance", task_head
            )
            publication = (["Justfile"], "22: maintenance", "canonical body")
            merged = {"mergeCommitOid": merge_oid}
            revision = "a" * 40

            def run_bridge() -> dict:
                with mock.patch.object(bridge, "_clean_root", return_value=revision), \
                     mock.patch.object(bridge, "_verify_bootstrap"), \
                     mock.patch.object(bridge, "_verified_modules") as modules, \
                     mock.patch.object(bridge, "maintenance_environment", return_value=nullcontext()), \
                     mock.patch.object(
                         sys,
                         "argv",
                         ["bridge", "maintenance-finalize", str(main), "22", "23", revision],
                     ), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    modules.return_value.__enter__.return_value = {
                        "maintenance_lifecycle": maintenance
                    }
                    self.assertEqual(bridge.main(), 0)
                return json.loads(output.getvalue())

            with mock.patch.object(
                maintenance, "_stored_contract", return_value=(record, {"repository": "upiscium/AgentKnowledgeVault"})
            ), mock.patch.object(
                maintenance, "_validate_consumed_receipt", return_value={"commit_sha": task_head}
            ) as receipts, mock.patch.object(
                maintenance, "_publication_evidence", return_value=publication
            ) as publications, mock.patch.object(
                maintenance, "_merged_pr", return_value=merged
            ) as prs, mock.patch.object(
                maintenance.lifecycle, "require_resolved_contract"
            ):
                first = run_bridge()
                first_state = state.read_bytes()
                second = run_bridge()

            self.assertEqual(self.git(main, "rev-parse", "HEAD"), merge_oid)
            self.assertEqual(first["transition"], "finalized")
            self.assertEqual(second["transition"], "already-finalized")
            self.assertEqual(state.read_bytes(), first_state)
            self.assertIn("- Status: merged", first_state.decode("utf-8"))
            self.assertEqual(first_state.count(b"### Maintenance publication"), 1)
            self.assertEqual(receipts.call_count, 6)
            self.assertEqual(publications.call_count, 6)
            self.assertEqual(prs.call_count, 6)


if __name__ == "__main__":
    unittest.main()
