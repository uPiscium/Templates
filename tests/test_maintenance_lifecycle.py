from __future__ import annotations

import importlib.util
import json
import subprocess
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

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def _init_repo(self, root: Path) -> None:
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.name", "Maintenance Test")
        self._git(root, "config", "user.email", "maintenance@example.invalid")

    def _commit(self, root: Path, message: str) -> str:
        self._git(root, "add", ".")
        self._git(root, "commit", "-m", message)
        return self._git(root, "rev-parse", "HEAD")

    def _write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

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

    def _receipt(self, **values) -> dict:
        return {
            "authority_head": self.BASE,
            "source": "/trusted/Templates",
            "source_revision": self.HEAD,
            "changed_paths": [".automation/bin/maintenance.py"],
            "path_fingerprints": {
                ".automation/bin/maintenance.py": {
                    "state": "file",
                    "mode": 0o644,
                    "content_sha256": "e" * 64,
                }
            },
            **values,
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
                    return_value=self._receipt(),
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
            receipt = self._receipt(commit_sha=self.HEAD)

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

    def test_consumed_receipt_uses_real_ancestry_and_rejects_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._write(root, "tracked.txt", "base\n")
            base = self._commit(root, "base")
            self._write(root, "tracked.txt", "older maintenance\n")
            older = self._commit(root, "older maintenance")
            self._write(root, "latest.txt", "latest maintenance\n")
            head = self._commit(root, "latest maintenance")
            self._git(root, "checkout", "-b", "diverged", base)
            self._write(root, "diverged.txt", "diverged\n")
            diverged = self._commit(root, "diverged")
            self._git(root, "checkout", "main")

            record = maintenance.lifecycle.WorktreeRecord(
                root, "task/21-agent-core-v3-1-5", head
            )
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = self._receipt(
                commit_sha=head, authority_head=base, source_revision=head
            )
            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=older),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["stage"], "committed")
            self.assertEqual(result["remoteRelation"], "ancestor")

            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=diverged),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                with self.assertRaisesRegex(
                    maintenance.MaintenanceError, "not the maintenance commit or its ancestor"
                ):
                    maintenance._maintenance_stage(record, "21", self._contract(root))

    def test_consumed_receipt_stages_resume_from_commit_push_and_draft_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = self._receipt(commit_sha=self.HEAD)
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
            receipt = self._receipt(commit_sha=self.HEAD)
            subject = maintenance._review_subject(receipt)
            objective = maintenance._review_objective("21", "reviewer", subject)
            with mock.patch.object(
                maintenance.lifecycle,
                "read_work_units",
                return_value={
                    "units": {
                        "WU-21-01": {
                            "requested_role": "reviewer",
                            "objective": objective,
                            "semantic_sha256": maintenance.lifecycle.semantic_digest(objective),
                            "state": "completed",
                            "transitions": [
                                {"to": "completed", "evidence_sha256": "f" * 64}
                            ],
                        }
                    }
                },
            ):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "security-reviewer"):
                    maintenance._require_review_evidence(record, "21", receipt)

    def test_review_evidence_is_bound_to_exact_maintenance_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            old_receipt = self._receipt(commit_sha=self.HEAD)
            new_receipt = self._receipt(
                commit_sha=self.HEAD,
                source_revision=self.MERGE,
                changed_paths=[".automation/bin/newer.py"],
                path_fingerprints={
                    ".automation/bin/newer.py": {
                        "state": "file",
                        "mode": 0o644,
                        "content_sha256": "1" * 64,
                    }
                },
            )
            old_subject = maintenance._review_subject(old_receipt)
            units = {}
            for index, role in enumerate(("reviewer", "security-reviewer"), start=1):
                objective = maintenance._review_objective("21", role, old_subject)
                units[f"WU-21-0{index}"] = {
                    "requested_role": role,
                    "objective": objective,
                    "semantic_sha256": maintenance.lifecycle.semantic_digest(objective),
                    "state": "completed",
                    "transitions": [
                        {"to": "completed", "evidence_sha256": str(index) * 64}
                    ],
                }
            with mock.patch.object(
                maintenance.lifecycle,
                "read_work_units",
                return_value={"units": units},
            ):
                with self.assertRaisesRegex(
                    maintenance.MaintenanceError, "reviewer, security-reviewer"
                ):
                    maintenance._require_review_evidence(record, "21", new_receipt)

    def test_main_only_review_recorder_persists_exact_subject_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root / "task")
            receipt = self._receipt(commit_sha=self.HEAD)
            units = {"schema_version": 1, "task_id": "21", "units": {}}
            evidence = "status: COMPLETED; no findings for the exact maintenance subject"
            with (
                mock.patch.object(maintenance.lifecycle, "require_main_worktree") as main_only,
                mock.patch.object(
                    maintenance, "_validated_contract", return_value=(record, self._contract(record.path))
                ),
                mock.patch.object(
                    maintenance, "_validate_consumed_receipt", return_value=receipt
                ),
                mock.patch.object(
                    maintenance.lifecycle, "work_units_lock", return_value=nullcontext()
                ),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(
                    maintenance.lifecycle, "read_work_units", return_value=units
                ),
                mock.patch.object(maintenance.lifecycle, "persist_work_units") as persist,
            ):
                result = maintenance.maintenance_review_record(
                    root, "21", "security-reviewer", evidence
                )
            main_only.assert_called_once_with(root)
            persisted = persist.call_args.args[1]
            unit = persisted["units"][result["workUnit"]]
            self.assertEqual(unit["state"], "completed")
            self.assertEqual(
                unit["objective"],
                maintenance._review_objective(
                    "21", "security-reviewer", maintenance._review_subject(receipt)
                ),
            )
            self.assertEqual(unit["transitions"][-1]["evidence"], evidence)

            with (
                mock.patch.object(maintenance.lifecycle, "require_main_worktree"),
                self.assertRaisesRegex(
                    maintenance.MaintenanceError, "must start with status: COMPLETED;"
                ),
            ):
                maintenance.maintenance_review_record(
                    root, "21", "security-reviewer", "status: BLOCKED; no result"
                )

    def test_pr_create_uses_full_direct_upgrade_diff_not_latest_receipt_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            body_path = root / ".task-state" / "pr-body.md"
            body_path.parent.mkdir(parents=True)
            body_path.write_text("body\n", encoding="utf-8")
            receipt = self._receipt(
                commit_sha=self.HEAD,
                changed_paths=[".automation/bin/second-upgrade-only.py"],
            )
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
                mock.patch.object(maintenance.publication, "verification_evidence"),
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

    def test_direct_upgrade_proof_covers_all_maintenance_commits_from_task_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            task = temporary / "task"
            source = temporary / "templates"
            task.mkdir()
            source.mkdir()
            self._init_repo(task)
            self._init_repo(source)

            self._write(task, ".automation/VERSION", "3\n")
            self._write(task, ".automation/bin/first-upgrade.py", "base\n")
            base = self._commit(task, "Task base")
            self._write(task, ".automation/bin/first-upgrade.py", "first upgrade\n")
            self._commit(task, "first maintenance upgrade")
            self._write(task, ".automation/bin/second-upgrade.py", "second upgrade\n")
            head = self._commit(task, "second maintenance upgrade")

            self._write(source, "components/agent-core/.automation/VERSION", "3\n")
            self._write(
                source,
                "components/agent-core/.automation/bin/first-upgrade.py",
                "first upgrade\n",
            )
            self._write(
                source,
                "components/agent-core/.automation/bin/second-upgrade.py",
                "second upgrade\n",
            )
            source_revision = self._commit(source, "final Templates source")

            self._state(task)
            state = task / ".task-state/task.md"
            state.write_text(
                state.read_text(encoding="utf-8")
                + f"\n## Provenance\n\n- Base revision: {base}\n",
                encoding="utf-8",
            )
            receipt = {
                "commit_sha": head,
                "source": str(source),
                "source_revision": source_revision,
                "changed_paths": [".automation/bin/second-upgrade.py"],
            }
            record = maintenance.lifecycle.WorktreeRecord(
                task, "task/21-agent-core-v3-1-5", head
            )

            paths = maintenance._direct_upgrade_publication(record, receipt)
            self.assertEqual(
                paths,
                [
                    ".automation/bin/first-upgrade.py",
                    ".automation/bin/second-upgrade.py",
                ],
            )

            (task / ".automation/bin/first-upgrade.py").write_text(
                "stale first upgrade\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                maintenance.MaintenanceError,
                "does not match the reconstructed direct upgrade",
            ):
                maintenance._direct_upgrade_publication(record, receipt)

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
            publication_evidence = ([".automation/bin/a.py"], "21: maintenance", "body")

            class Result:
                returncode = 0

            with (
                mock.patch.object(maintenance.lifecycle, "require_main_worktree"),
                mock.patch.object(maintenance, "_stored_contract", return_value=(record, contract)),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(
                    maintenance,
                    "_publication_evidence",
                    side_effect=[publication_evidence, publication_evidence],
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

    def test_publication_evidence_rejects_stale_verification_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            receipt = self._receipt(commit_sha=self.HEAD)
            with (
                mock.patch.object(maintenance, "_require_review_evidence"),
                mock.patch.object(
                    maintenance.publication,
                    "verification_evidence",
                    side_effect=maintenance.publication.PublicationMetadataError(
                        "project verification evidence is stale"
                    ),
                ),
                self.assertRaisesRegex(maintenance.MaintenanceError, "verification evidence is stale"),
            ):
                maintenance._publication_evidence(record, "21", receipt)

    def test_merged_pr_requires_canonical_title_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            details = {
                "number": 22,
                "title": "stale title",
                "body": "canonical body",
                "headRefName": record.branch,
                "baseRefName": "main",
                "headRefOid": self.HEAD,
                "isCrossRepository": False,
                "state": "MERGED",
                "mergeCommit": {"oid": self.MERGE},
            }
            with (
                mock.patch.object(maintenance.agent_core, "pr_details", return_value=details),
                mock.patch.object(maintenance.lifecycle, "default_branch", return_value="main"),
                mock.patch.object(
                    maintenance.agent_core,
                    "canonical_repository",
                    return_value="example/repo",
                ),
                self.assertRaisesRegex(maintenance.MaintenanceError, "title"),
            ):
                maintenance._merged_pr(
                    root,
                    record,
                    "example/repo",
                    22,
                    self.HEAD,
                    "21: canonical title",
                    "canonical body",
                )

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
        self.assertEqual(bash["just automation::maintenance-review-record *"], "deny")
        self.assertEqual(bash["just automation::maintenance-pr-create *"], "deny")
        self.assertEqual(bash["just automation::maintenance-finalize *"], "deny")
        self.assertEqual(bash["just automation::upgrade *"], "ask")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["git push *"], "deny")
        build = (
            ROOT / "components" / "agent-core" / ".opencode" / "agents" / "build.md"
        ).read_text(encoding="utf-8")
        self.assertIn('"just automation::maintenance-review-record *": allow', build)
        self.assertIn('"just automation::maintenance-pr-create *": deny', build)
        self.assertIn('"just automation::maintenance-finalize *": allow', build)
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
        self.assertIn("model: openai/gpt-5.6-sol", agent)
        self.assertIn('"just automation::maintenance-review-record *": deny', agent)
        self.assertIn('"just automation::maintenance-pr-create *": allow', agent)
        self.assertIn('"just automation::maintenance-finalize *": deny', agent)
        self.assertNotIn("reviewer: allow", agent)
        self.assertNotIn("security-reviewer: allow", agent)
        self.assertNotIn('"just agent::work-unit-state-set *": allow', agent)

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
