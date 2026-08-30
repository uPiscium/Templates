from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "components" / "agent-core" / ".automation" / "bin"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SCRIPT = SCRIPT_DIR / "maintenance_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("maintenance_lifecycle_for_tests", SCRIPT)
assert SPEC and SPEC.loader
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


class MaintenanceLifecycleTest(unittest.TestCase):
    HEAD = "a" * 40
    BASE = "b" * 40
    OLD_REMOTE = "9" * 40
    MERGE = "c" * 40

    def _record(self, root: Path):
        return maintenance.lifecycle.WorktreeRecord(
            root,
            "task/21-agent-core-v3-1-5",
            self.HEAD,
        )

    def _contract(self, root: Path) -> dict:
        return {
            "status": "READY",
            "task": "21",
            "worktree": str(root),
            "issue": 21,
            "repository": "example/repo",
            "sha256": "d" * 64,
        }

    def _state(self, root: Path, status: str = "initialized") -> None:
        state = root / ".task-state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "task.md").write_text(
            f"""# Task 21

## Identity

- Task ID: 21
- Branch: task/21-agent-core-v3-1-5
- Worktree: {root}

## Current state

- Status: {status}
- Blockers: none
- Unverified: none
""",
            encoding="utf-8",
        )

    def test_applied_stage_uses_active_receipt_without_normal_resume_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            active.write_text("{}\n", encoding="utf-8")
            consumed = root / "consumed.json"
            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(
                    maintenance,
                    "_validate_active_receipt",
                    return_value={"source_revision": self.HEAD},
                ),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["mode"], "maintenance")
            self.assertEqual(result["stage"], "applied")
            self.assertEqual(result["taskStatus"], "initialized")

    def test_consumed_receipt_resumes_when_remote_is_old_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = {"commit_sha": self.HEAD, "source_revision": self.BASE}

            class Result:
                returncode = 0

            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=self.OLD_REMOTE),
                mock.patch.object(maintenance.lifecycle, "run", return_value=Result()),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["stage"], "committed")
            self.assertEqual(result["remoteHead"], self.OLD_REMOTE)
            self.assertEqual(result["remoteRelation"], "ancestor")

    def test_consumed_receipt_stages_resume_from_commit_push_and_draft_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = {"commit_sha": self.HEAD, "source_revision": self.BASE}
            cases = (
                (None, None, "committed"),
                (self.HEAD, None, "pushed"),
                (
                    self.HEAD,
                    {"number": 22, "state": "OPEN", "isDraft": True},
                    "draft-pr-created",
                ),
                (
                    None,
                    {"number": 22, "state": "MERGED", "isDraft": False},
                    "merged-remote",
                ),
            )
            for remote, pr, expected in cases:
                with self.subTest(stage=expected):
                    with (
                        mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                        mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                        mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                        mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                        mock.patch.object(maintenance, "_remote_head", return_value=remote),
                        mock.patch.object(maintenance, "_pr_evidence", return_value=pr),
                    ):
                        result = maintenance._maintenance_stage(record, "21", self._contract(root))
                    self.assertEqual(result["stage"], expected)
                    self.assertEqual(result["taskStatus"], "initialized")

    def test_review_evidence_requires_reviewer_and_security_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            with mock.patch.object(
                maintenance.lifecycle,
                "read_work_units",
                return_value={
                    "units": {
                        "WU-21-01": {
                            "requested_role": "reviewer",
                            "state": "completed",
                        }
                    }
                },
            ):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "security-reviewer"):
                    maintenance._require_review_evidence(record, "21")

    def test_pr_create_uses_full_direct_upgrade_diff_not_latest_receipt_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            body_path = root / ".task-state" / "pr-body.md"
            body_path.parent.mkdir(parents=True)
            body_path.write_text("body\n", encoding="utf-8")
            receipt = {
                "commit_sha": self.HEAD,
                "changed_paths": [".automation/bin/second-upgrade-only.py"],
            }
            full_paths = [
                ".automation/bin/first-upgrade.py",
                ".automation/bin/second-upgrade-only.py",
            ]
            live = {
                "number": 22,
                "title": "21: maintenance",
                "body": "body",
                "headRefName": record.branch,
                "baseRefName": "main",
                "headRefOid": self.HEAD,
                "isDraft": True,
                "isCrossRepository": False,
                "state": "OPEN",
            }
            with (
                mock.patch.object(maintenance.lifecycle, "require_local_task", return_value=record),
                mock.patch.object(
                    maintenance,
                    "maintenance_check",
                    return_value={**self._contract(root), "stage": "pushed", "mode": "maintenance"},
                ),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=self.HEAD),
                mock.patch.object(maintenance, "_require_review_evidence"),
                mock.patch.object(maintenance.agent_core, "verify"),
                mock.patch.object(maintenance, "_direct_upgrade_publication", return_value=full_paths),
                mock.patch.object(
                    maintenance.publication,
                    "canonical_metadata",
                    return_value=("21: maintenance", "body\n"),
                ) as metadata,
                mock.patch.object(maintenance.publication, "write_metadata"),
                mock.patch.object(
                    maintenance.agent_core,
                    "_validated_local_metadata",
                    return_value=("21: maintenance", body_path, "body"),
                ),
                mock.patch.object(maintenance.agent_core, "default_branch", return_value="main"),
                mock.patch.object(maintenance.agent_core, "pr_for_branch", side_effect=[live, live]),
                mock.patch.object(maintenance.agent_core, "canonical_repository", return_value="example/repo"),
                mock.patch.object(maintenance.agent_core, "_validate_live_pr"),
                mock.patch.object(maintenance.agent_core, "gh") as gh_mock,
            ):
                result = maintenance.maintenance_pr_create(root, "21")
            gh_mock.assert_not_called()
            metadata.assert_called_once_with(
                root, "21", head=self.HEAD, changed_paths=full_paths
            )
            self.assertEqual(result["stage"], "draft-pr-created")
            self.assertEqual(result["pr"], 22)

    def test_finalize_revalidates_direct_upgrade_and_uses_dedicated_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "task"
            task_root.mkdir()
            record = self._record(task_root)
            contract = self._contract(task_root)
            receipt = {"commit_sha": self.HEAD}
            merged = {"mergeCommitOid": self.MERGE}
            sync = {"branch": "main", "revision": "e" * 40, "updated": True}
            publication_paths = [".automation/bin/a.py"]

            class Result:
                returncode = 0

            with (
                mock.patch.object(maintenance.lifecycle, "require_main_worktree"),
                mock.patch.object(maintenance, "_stored_contract", return_value=(record, contract)),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(
                    maintenance,
                    "_direct_upgrade_publication",
                    side_effect=[publication_paths, publication_paths],
                ) as reconstruct,
                mock.patch.object(maintenance, "_merged_pr", side_effect=[merged, merged]),
                mock.patch.object(maintenance.lifecycle, "synchronize_default_branch", return_value=sync),
                mock.patch.object(maintenance.lifecycle, "run", return_value=Result()),
                mock.patch.object(maintenance.lifecycle, "require_synchronized_default_branch_revision"),
                mock.patch.object(maintenance, "_mark_maintenance_merged", return_value="finalized") as mark,
                mock.patch.object(maintenance.lifecycle, "append_task_evidence"),
            ):
                result = maintenance.maintenance_finalize(root, "21", 22)
            self.assertEqual(reconstruct.call_count, 2)
            mark.assert_called_once_with(record, "21")
            self.assertEqual(result["status"], "FINALIZED")
            self.assertEqual(result["stage"], "merged")

    def test_dedicated_terminal_transition_does_not_change_normal_transition_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._state(root, "initialized")
            record = self._record(root)
            with (
                mock.patch.object(maintenance.lifecycle, "require_resolved_contract"),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(maintenance.lifecycle, "work_units_lock", return_value=nullcontext()),
            ):
                result = maintenance._mark_maintenance_merged(record, "21")
            self.assertEqual(result, "finalized")
            self.assertEqual(
                maintenance.lifecycle.state_status(root / ".task-state" / "task.md"),
                "merged",
            )
            self.assertNotIn("merged", maintenance.lifecycle.LINEAR_TRANSITIONS["initialized"])

    def test_permission_and_command_surfaces_are_explicit(self) -> None:
        config = json.loads(
            (ROOT / "components" / "agent-core" / "opencode.json").read_text(encoding="utf-8")
        )
        bash = config["permission"]["bash"]
        self.assertEqual(bash["just automation::maintenance-check *"], "allow")
        self.assertEqual(bash["just automation::maintenance-pr-create *"], "allow")
        self.assertEqual(bash["just automation::maintenance-finalize *"], "allow")
        self.assertEqual(bash["just automation::upgrade *"], "ask")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["git push *"], "deny")
        command = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "commands"
            / "maintenance-run.md"
        ).read_text(encoding="utf-8")
        self.assertIn("maintenance-orchestration", command)
        self.assertIn("maintenance-check", command)
        agent = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "agents"
            / "maintenance-orchestrator.md"
        ).read_text(encoding="utf-8")
        self.assertIn('model: "openai/gpt-5.6-sol"', agent)

    def test_generated_maintenance_surface_matches_canonical(self) -> None:
        canonical = ROOT / "components" / "agent-core"
        for template in (
            "agent-base",
            "agent-python",
            "agent-rust",
            "agent-nix",
            "agent-cpp-cmake",
        ):
            generated = ROOT / "templates" / template
            for relative in (
                ".automation/bin/maintenance_lifecycle.py",
                ".automation/just/automation.just",
                ".opencode/agents/build.md",
                ".opencode/agents/maintenance-orchestrator.md",
                ".opencode/commands/maintenance-run.md",
                ".opencode/skills/maintenance-orchestration/SKILL.md",
                "opencode.json",
            ):
                self.assertEqual(
                    (generated / relative).read_bytes(),
                    (canonical / relative).read_bytes(),
                    f"{template}: {relative}",
                )


if __name__ == "__main__":
    unittest.main()
