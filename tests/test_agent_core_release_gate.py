from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "agent_core_release_gate.py"
spec = importlib.util.spec_from_file_location("agent_core_release_gate", MODULE_PATH)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


class ReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        run_git(self.root, "init", "-q")
        run_git(self.root, "config", "user.email", "test@example.invalid")
        run_git(self.root, "config", "user.name", "Release Gate Test")
        (self.root / "file").write_text("one\n", encoding="utf-8")
        run_git(self.root, "add", "file")
        run_git(self.root, "commit", "-qm", "one")
        run_git(self.root, "remote", "add", "origin", gate.CANONICAL_GIT_URL)
        self.head = run_git(self.root, "rev-parse", "HEAD")
        self.tree = run_git(self.root, "rev-parse", "HEAD^{tree}")
        (self.root / ".worktrees").mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def pr(self, *, head: str | None = None, state: str = "open", merged: bool = False, merge: str | None = None) -> dict:
        return {
            "state": state, "merged": merged, "merge_commit_sha": merge,
            "base": {"ref": "main", "repo": {"full_name": gate.CANONICAL_REPO}},
            "head": {"sha": head or self.head, "repo": {"full_name": gate.CANONICAL_REPO}},
        }

    def workflow_run(
        self,
        *,
        head: str | None = None,
        status: str = "completed",
        conclusion: str | None = "success",
        run_id: int = 700,
        attempt: int = 1,
    ) -> dict:
        return {
            "id": run_id,
            "workflow_id": 600,
            "path": gate.CANONICAL_WORKFLOW_PATH,
            "event": "pull_request",
            "head_sha": head or self.head,
            "run_attempt": attempt,
            "status": status,
            "conclusion": conclusion,
            "pull_requests": [
                {
                    "number": 115,
                    "head": {
                        "sha": head or self.head,
                        "repo": {"url": f"https://api.github.com/repos/{gate.CANONICAL_REPO}"},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"url": f"https://api.github.com/repos/{gate.CANONICAL_REPO}"},
                    },
                }
            ],
        }

    def gh_candidate(
        self,
        *,
        pr_values=None,
        rollup=None,
        commit_trees=None,
        comparison=None,
        workflow_runs=None,
        workflow_detail=None,
        workflow=None,
    ):
        values = iter(pr_values or [self.pr(), self.pr(), self.pr(), self.pr()])
        trees = commit_trees or {self.head: self.tree}
        runs = [self.workflow_run()] if workflow_runs is None else workflow_runs
        detail = workflow_detail or (runs[0] if runs else self.workflow_run())
        workflow_metadata = workflow or {
            "id": 600,
            "path": gate.CANONICAL_WORKFLOW_PATH,
            "state": "active",
        }
        rollup = rollup or {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": {
            "oid": self.head, "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ]}}
        }}]}}}}}

        def fake(args, cwd):
            if args[:2] == ["api", "graphql"]:
                return rollup
            endpoint = args[1]
            if endpoint.endswith("/actions/workflows/template-ci.yml"):
                return workflow_metadata
            if "/actions/workflows/600/runs?" in endpoint:
                return {"total_count": len(runs), "workflow_runs": runs}
            if endpoint.endswith("/actions/runs/700"):
                return detail
            if "/pulls/" in endpoint:
                return next(values)
            if "/git/commits/" in endpoint:
                sha = endpoint.rsplit("/", 1)[1]
                return {"sha": sha, "tree": {"sha": trees[sha]}}
            if "/compare/" in endpoint:
                return comparison or {"status": "identical"}
            raise AssertionError(args)
        return fake

    def test_exact_open_head_tree_and_successful_ci_ready(self) -> None:
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            result = gate.candidate(self.root, "115")
        self.assertEqual({"pr": 115, "head": self.head, "tree": self.tree, "base": "main", "ci": "PASS", "status": "READY_FOR_DOGFOOD"}, result)

    def test_only_unrelated_successful_rollup_without_template_ci_is_blocked(self) -> None:
        with mock.patch.object(
            gate,
            "gh",
            side_effect=self.gh_candidate(workflow_runs=[]),
        ):
            with self.assertRaisesRegex(gate.GateError, "Template CI workflow is missing"):
                gate.candidate(self.root, "115")

    def test_template_ci_for_older_head_is_blocked(self) -> None:
        older = "a" * 40
        with mock.patch.object(
            gate,
            "gh",
            side_effect=self.gh_candidate(workflow_runs=[self.workflow_run(head=older)]),
        ):
            with self.assertRaisesRegex(gate.GateError, "exact candidate"):
                gate.candidate(self.root, "115")

    def test_template_ci_pending_and_failed_are_blocked(self) -> None:
        for run in (
            self.workflow_run(status="in_progress", conclusion=None),
            self.workflow_run(status="completed", conclusion="failure"),
        ):
            with self.subTest(status=run["status"], conclusion=run["conclusion"]):
                with mock.patch.object(
                    gate,
                    "gh",
                    side_effect=self.gh_candidate(workflow_runs=[run], workflow_detail=run),
                ):
                    with self.assertRaisesRegex(gate.GateError, "incomplete or unsuccessful"):
                        gate.candidate(self.root, "115")

    def test_multiple_exact_head_template_ci_runs_are_ambiguous(self) -> None:
        with mock.patch.object(
            gate,
            "gh",
            side_effect=self.gh_candidate(
                workflow_runs=[self.workflow_run(), self.workflow_run(run_id=701)]
            ),
        ):
            with self.assertRaisesRegex(gate.GateError, "ambiguous"):
                gate.candidate(self.root, "115")

    def test_template_ci_for_another_pr_with_same_head_is_blocked(self) -> None:
        run = self.workflow_run()
        run["pull_requests"][0]["number"] = 999
        with mock.patch.object(
            gate,
            "gh",
            side_effect=self.gh_candidate(workflow_runs=[run], workflow_detail=run),
        ):
            with self.assertRaisesRegex(gate.GateError, "another pull request"):
                gate.candidate(self.root, "115")

    def test_pending_and_failed_ci_are_blocked(self) -> None:
        for context in (
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "StatusContext", "state": "FAILURE"},
        ):
            rollup = {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": {
                "oid": self.head, "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [context]}}
            }}]}}}}}
            with mock.patch.object(gate, "gh", side_effect=self.gh_candidate(rollup=rollup)):
                with self.assertRaisesRegex(gate.GateError, "incomplete or unsuccessful"):
                    gate.candidate(self.root, "115")

    def test_head_change_during_inspection_is_blocked(self) -> None:
        changed = "b" * 40
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate(pr_values=[self.pr(), self.pr(head=changed)])):
            with self.assertRaisesRegex(gate.GateError, "moved"):
                gate.candidate(self.root, "115")

    def test_detached_exact_clean_worktree_is_created_and_verified(self) -> None:
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()), mock.patch.object(
            gate, "CANONICAL_GIT_URL", self.root.as_uri()
        ):
            result = gate.create_or_verify_worktree(self.root, "115", "rc")
        path = self.root / ".worktrees" / "rc"
        self.assertEqual(result["path"], str(path))
        self.assertEqual(run_git(path, "rev-parse", "HEAD"), self.head)
        self.assertEqual(gate.git(["symbolic-ref", "-q", "HEAD"], path, check=False).strip(), "")
        self.assertEqual(run_git(path, "status", "--porcelain"), "")
        self.assertEqual(
            gate.safe_worktree_path(self.root, ".worktrees/rc", create_parents=False),
            path,
        )

    def test_registered_wrong_revision_is_blocked_and_unchanged(self) -> None:
        other = self.root / "other"
        (other / "x").parent.mkdir()
        (other / "x").write_text("x", encoding="utf-8")
        run_git(self.root, "add", "other")
        run_git(self.root, "commit", "-qm", "other")
        wrong = run_git(self.root, "rev-parse", "HEAD")
        path = self.root / ".worktrees" / "rc"
        run_git(self.root, "worktree", "add", "--detach", str(path), wrong)
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "another revision"):
                gate.create_or_verify_worktree(self.root, "115", "rc")
        self.assertEqual(run_git(path, "rev-parse", "HEAD"), wrong)

    def test_dirty_and_unregistered_existing_worktrees_are_rejected(self) -> None:
        path = self.root / ".worktrees" / "rc"
        path.mkdir()
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "exact registered worktree root"):
                gate.create_or_verify_worktree(self.root, "115", "rc")
        path.rmdir()
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()), mock.patch.object(
            gate, "CANONICAL_GIT_URL", self.root.as_uri()
        ):
            gate.create_or_verify_worktree(self.root, "115", "rc")
        (path / "dirty").write_text("dirty", encoding="utf-8")
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "not clean"):
                gate.create_or_verify_worktree(self.root, "115", "rc")

    def test_existing_candidate_with_unsafe_config_is_rejected_before_verification(self) -> None:
        path = self.root / ".worktrees" / "rc"
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()), mock.patch.object(
            gate, "CANONICAL_GIT_URL", self.root.as_uri()
        ):
            gate.create_or_verify_worktree(self.root, "115", "rc")
        run_git(path, "config", "--local", "filter.attack.process", "attacker-process")
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "unsafe local Git configuration"):
                gate.create_or_verify_worktree(self.root, "115", "rc")

    def evidence(self, *, head=None, tree=None, operation="dogfood") -> dict:
        return {"schema": "agent-core-release-gate", "version": 1, "repo": gate.CANONICAL_REPO,
                "pr": 115, "head": head or self.head, "tree": tree or self.tree,
                "downstreamRepo": gate.DOWNSTREAM_REPO, "task": 116, "downstreamPr": 7,
                "outcome": "PASS", "operation": operation, "recordedAt": "2026-09-01T00:00:00Z"}

    def write_evidence(self, *records: dict) -> None:
        directory = gate.evidence_root(self.root, create=True)
        start = len(list(directory.iterdir()))
        for index, record in enumerate(records, start=start):
            (directory / f"{index}.json").write_text(json.dumps(record) + "\n", encoding="utf-8")
            os.chmod(directory / f"{index}.json", 0o600)

    def test_recorded_pass_evidence_contains_all_identity_bindings(self) -> None:
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            record = gate.record_evidence(self.root, "115", "dogfood-run", "116", "7")
        self.assertEqual(record["repo"], gate.CANONICAL_REPO)
        self.assertEqual(record["downstreamRepo"], gate.DOWNSTREAM_REPO)
        self.assertEqual((record["pr"], record["head"], record["tree"]), (115, self.head, self.tree))
        self.assertEqual((record["task"], record["downstreamPr"], record["operation"]), (116, 7, "dogfood-run"))
        self.assertEqual(record["outcome"], "PASS")
        self.assertTrue(record["recordedAt"].endswith("Z"))
        records = list(gate.evidence_root(self.root, create=False).iterdir())
        self.assertEqual(len(records), 1)
        self.assertRegex(records[0].name, r"^[0-9a-f]{64}\.json$")
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "already exists"):
                gate.record_evidence(self.root, "115", "second-run", "116", "7")
        self.assertEqual(len(list(gate.evidence_root(self.root, create=False).iterdir())), 1)

    def test_launcher_rejects_python_from_untrusted_path(self) -> None:
        fake_bin = Path(self.temp.name) / "fake-bin"
        fake_bin.mkdir()
        marker = Path(self.temp.name) / "executed"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            f"#!/bin/sh\ntouch '{marker}'\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        launcher = MODULE_PATH.with_name("run_agent_core_release_gate.sh")
        result = subprocess.run(
            ["/bin/sh", str(launcher), str(MODULE_PATH), "release-candidate-check", "115"],
            env={"PATH": str(fake_bin)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not trusted", result.stderr)
        self.assertFalse(marker.exists())

    def test_subprocess_environment_discards_transport_overrides(self) -> None:
        hostile = {
            "HOME": "/safe-home",
            "GH_TOKEN": "token",
            "PATH": "/hostile",
            "HTTPS_PROXY": "https://attacker.invalid",
            "SSL_CERT_FILE": "/attacker-ca",
            "LD_PRELOAD": "/attacker.so",
        }
        with mock.patch.dict(os.environ, hostile, clear=True):
            environment = gate.sanitized_environment()
        self.assertEqual(environment["HOME"], "/safe-home")
        self.assertEqual(environment["GH_TOKEN"], "token")
        for key in ("PATH", "HTTPS_PROXY", "SSL_CERT_FILE", "LD_PRELOAD"):
            self.assertNotIn(key, environment)

    def test_github_api_is_pinned_to_public_github(self) -> None:
        response = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(gate, "command", return_value=response) as runner:
            gate.gh(["api", "repos/upiscium/Templates/pulls/115"], self.root)
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "api", "--hostname", "github.com"])

    def test_local_git_configuration_and_canonical_origin_are_required(self) -> None:
        self.assertEqual(
            gate.validate_local_git_configuration(self.root),
            gate.CANONICAL_GIT_URL,
        )
        run_git(self.root, "remote", "set-url", "origin", "https://example.invalid/Templates.git")
        with self.assertRaisesRegex(gate.GateError, "canonical Templates"):
            gate.validate_local_git_configuration(self.root)

    def test_unsafe_local_git_execution_and_transport_configuration_is_rejected(self) -> None:
        unsafe = (
            ("core.sshCommand", "attacker-ssh"),
            ("url.ssh://attacker.invalid/.insteadOf", "https://github.com/"),
            ("credential.helper", "attacker-helper"),
            ("filter.attack.smudge", "attacker-smudge"),
            ("filter.attack.process", "attacker-process"),
            ("remote.origin.uploadpack", "attacker-upload-pack"),
            ("include.path", "/tmp/attacker-config"),
        )
        for name, value in unsafe:
            with self.subTest(name=name):
                run_git(self.root, "config", "--local", name, value)
                try:
                    with self.assertRaisesRegex(gate.GateError, "unsafe local Git configuration"):
                        gate.validate_local_git_configuration(self.root)
                finally:
                    run_git(self.root, "config", "--local", "--unset-all", name)

    def test_worktree_refuses_unsafe_config_before_checkout(self) -> None:
        run_git(self.root, "config", "--local", "filter.attack.smudge", "attacker-smudge")
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            with self.assertRaisesRegex(gate.GateError, "unsafe local Git configuration"):
                gate.create_or_verify_worktree(self.root, "115", "unsafe")
        self.assertFalse((self.root / ".worktrees" / "unsafe").exists())

    def test_unsafe_worktree_scoped_git_configuration_is_rejected(self) -> None:
        run_git(self.root, "config", "--local", "extensions.worktreeConfig", "true")
        run_git(self.root, "config", "--worktree", "filter.attack.process", "attacker-process")
        with self.assertRaisesRegex(gate.GateError, "unsafe worktree Git configuration"):
            gate.validate_local_git_configuration(self.root)

    def merge_gh(self, merge: str, merge_tree: str, *, evidence=None, pr_values=None, comparison=None):
        values = iter(pr_values or [self.pr(state="closed", merged=True, merge=merge)] * 4)
        trees = {self.head: self.tree, merge: merge_tree}
        def fake(args, cwd):
            if args[:2] == ["api", "graphql"]:
                raise AssertionError("graphql must be routed to the rollup fixture")
            endpoint = args[1]
            if endpoint.endswith("/actions/workflows/template-ci.yml"):
                return {"id": 600, "path": gate.CANONICAL_WORKFLOW_PATH, "state": "active"}
            if "/actions/workflows/600/runs?" in endpoint:
                run = self.workflow_run()
                return {"total_count": 1, "workflow_runs": [run]}
            if endpoint.endswith("/actions/runs/700"):
                return self.workflow_run()
            if "/pulls/" in endpoint:
                return next(values)
            if "/git/commits/" in endpoint:
                sha = endpoint.rsplit("/", 1)[1]
                return {"sha": sha, "tree": {"sha": trees[sha]}}
            if "/compare/" in endpoint:
                return comparison or {"status": "identical"}
            raise AssertionError(args)
        # The post-merge candidate still requires a successful rollup for the head.
        rollup = {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": {
            "oid": self.head, "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}
        }}]}}}}}
        def routed(args, cwd):
            if args[:2] == ["api", "graphql"]: return rollup
            return fake(args, cwd)
        return routed

    def test_pass_evidence_binds_identity_and_wrong_identity_is_blocked(self) -> None:
        record = self.evidence()
        self.write_evidence(record)
        merge = "c" * 40
        with mock.patch.object(gate, "gh", side_effect=self.merge_gh(merge, self.tree)):
            result = gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")
        self.assertEqual(result["status"], "RELEASE_READY")
        for key, value in (("repo", "other/repo"), ("pr", 999), ("downstreamRepo", "other/downstream"), ("head", "d" * 40), ("tree", "e" * 40)):
            bad = self.evidence(); bad[key] = value
            directory = gate.evidence_root(self.root, create=False)
            (directory / "0.json").write_text(json.dumps(bad), encoding="utf-8")
            with mock.patch.object(gate, "gh", side_effect=self.merge_gh(merge, self.tree)):
                with self.assertRaises(gate.GateError): gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")
            (directory / "0.json").write_text(json.dumps(record), encoding="utf-8")

    def test_old_evidence_moved_head_and_duplicate_conflict_fail_closed(self) -> None:
        moved = "f" * 40
        self.write_evidence(self.evidence())
        moved_merge = "e" * 40
        moved_pr = self.pr(head=moved, state="closed", merged=True, merge=moved_merge)
        with mock.patch.object(
            gate,
            "gh",
            side_effect=self.merge_gh(
                moved_merge,
                "a" * 40,
                pr_values=[moved_pr, moved_pr, moved_pr, moved_pr],
            ),
        ):
            with self.assertRaisesRegex(gate.GateError, "dogfooded head"):
                gate.post_merge_gate(self.root, "115", moved_merge, self.head, self.tree, "")
        self.write_evidence(self.evidence(operation="other"))
        merge = "c" * 40
        with mock.patch.object(gate, "gh", side_effect=self.merge_gh(merge, self.tree)):
            with self.assertRaisesRegex(gate.GateError, "duplicated"):
                gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")

    def test_stale_rollup_oid_is_blocked(self) -> None:
        rollup = {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": {
            "oid": "a" * 40, "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}
        }}]}}}}}
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate(rollup=rollup)):
            with self.assertRaisesRegex(gate.GateError, "stale"):
                gate.candidate(self.root, "115")

    def test_squash_merge_same_tree_accepted_and_different_tree_blocked(self) -> None:
        merge = "c" * 40
        self.write_evidence(self.evidence())
        with mock.patch.object(gate, "gh", side_effect=self.merge_gh(merge, self.tree)):
            self.assertTrue(gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")["treeMatch"])
        with mock.patch.object(gate, "gh", side_effect=self.merge_gh(merge, "d" * 40)):
            with self.assertRaisesRegex(gate.GateError, "does not match"):
                gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")

    def test_main_containment_compare_uses_merge_as_base(self) -> None:
        merge = "c" * 40
        self.write_evidence(self.evidence())
        observed = []
        routed = self.merge_gh(merge, self.tree)

        def inspect(args, cwd):
            if len(args) > 1 and "/compare/" in args[1]:
                observed.append(args[1])
            return routed(args, cwd)

        with mock.patch.object(gate, "gh", side_effect=inspect):
            gate.post_merge_gate(self.root, "115", merge, self.head, self.tree, "")
        self.assertEqual(
            observed,
            [f"repos/{gate.CANONICAL_REPO}/compare/{merge}...main"],
        )

    def test_path_traversal_and_symlink_are_rejected(self) -> None:
        with mock.patch.object(gate, "gh", side_effect=self.gh_candidate()):
            for destination in ("../escape", "/tmp/outside"):
                with self.assertRaises(gate.GateError): gate.create_or_verify_worktree(self.root, "115", destination)
        link = self.root / ".worktrees" / "link"
        link.symlink_to(self.root)
        with self.assertRaises(gate.GateError): gate.safe_worktree_path(self.root, "link", create_parents=True)

    def test_release_and_tag_must_both_be_absent(self) -> None:
        cases = (
            ([200], "release already exists"),
            ([404, 200], "Git tag already exists"),
            ([500], "release lookup returned unexpected"),
            ([404, 500], "Git tag lookup returned unexpected"),
        )
        for statuses, message in cases:
            with self.subTest(statuses=statuses):
                with mock.patch.object(gate, "gh_status", side_effect=statuses):
                    with self.assertRaisesRegex(gate.GateError, message):
                        gate.release_absent(self.root, "v1.2.3")
        with mock.patch.object(gate, "gh_status", side_effect=[404, 404]) as status:
            gate.release_absent(self.root, "v1.2.3")
        self.assertEqual(
            [call.args[0][1] for call in status.call_args_list],
            [
                f"repos/{gate.CANONICAL_REPO}/releases/tags/v1.2.3",
                f"repos/{gate.CANONICAL_REPO}/git/ref/tags/v1.2.3",
            ],
        )


if __name__ == "__main__":
    unittest.main()
