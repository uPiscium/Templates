from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components/agent-core/.automation/bin"
sys.path.insert(0, str(BIN))
import task_lifecycle as lifecycle

spec = importlib.util.spec_from_file_location("post_merge_agent_core", BIN / "agent_core.py")
assert spec and spec.loader
agent_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_core)


def command(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        top = Path(self.temporary.name)
        self.remote = top / "origin.git"
        self.repo = top / "repo"
        self.publisher = top / "publisher"
        command("git", "init", "--bare", "--initial-branch=main", str(self.remote), cwd=top)
        command("git", "init", "--initial-branch=main", str(self.repo), cwd=top)
        self.configure(self.repo)
        (self.repo / ".automation/templates").mkdir(parents=True)
        (self.repo / ".automation/templates/task-state.md").write_text(
            "- Task ID: @@TASK_ID@@\n"
            "- Branch: @@BRANCH@@\n"
            "- Worktree: @@WORKTREE@@\n"
            "- Base branch: @@BASE_BRANCH@@\n"
            "- Base revision: @@BASE_REVISION@@\n"
            "- Status: initialized\n",
            encoding="utf-8",
        )
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "initial", cwd=self.repo)
        command("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        command("git", "push", "-u", "origin", "main", cwd=self.repo)
        command("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        exclude = Path(command("git", "rev-parse", "--git-common-dir", cwd=self.repo)) / "info/exclude"
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("/.worktrees/\n/.task-state/\n", encoding="utf-8")
        command("git", "clone", str(self.remote), str(self.publisher), cwd=top)
        self.configure(self.publisher)

    @staticmethod
    def configure(repo: Path) -> None:
        command("git", "config", "user.name", "Test User", cwd=repo)
        command("git", "config", "user.email", "test@example.invalid", cwd=repo)

    def publish(self, text: str) -> str:
        path = self.publisher / "tracked.txt"
        path.write_text(path.read_text(encoding="utf-8") + text + "\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.publisher)
        command("git", "commit", "-m", text, cwd=self.publisher)
        command("git", "push", "origin", "main", cwd=self.publisher)
        return command("git", "rev-parse", "HEAD", cwd=self.publisher)

    def start_task(self, task: str = "TASK-1", slug: str = "demo") -> Path:
        lifecycle.task_start(self.repo, task, slug)
        return self.repo / ".worktrees" / f"{task}-{slug}"


class DefaultBranchSynchronizationTest(RepositoryFixture):
    def test_fetches_only_origin_default_and_fast_forwards(self) -> None:
        expected = self.publish("remote advance")
        with mock.patch.object(lifecycle, "run", wraps=lifecycle.run) as observed:
            result = lifecycle.synchronize_default_branch(self.repo)
        fetches = [call.args[0] for call in observed.call_args_list if call.args[0][:2] == ["git", "fetch"]]
        self.assertEqual(
            fetches,
            [["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"]],
        )
        self.assertEqual(result["revision"], expected)
        self.assertEqual(command("git", "rev-parse", "main", cwd=self.repo), expected)
        self.assertEqual(command("git", "rev-parse", "origin/main", cwd=self.repo), expected)

    def test_already_synchronized_is_idempotent(self) -> None:
        first = lifecycle.synchronize_default_branch(self.repo)
        second = lifecycle.synchronize_default_branch(self.repo)
        self.assertFalse(first["updated"])
        self.assertFalse(second["updated"])
        self.assertEqual(first["revision"], second["revision"])

    def test_dirty_tracked_and_untracked_main_fail_closed(self) -> None:
        for name in ("tracked.txt", "untracked.txt"):
            with self.subTest(name=name):
                path = self.repo / name
                original = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text("dirty\n", encoding="utf-8")
                with self.assertRaisesRegex(lifecycle.LifecycleError, "must be clean"):
                    lifecycle.synchronize_default_branch(self.repo)
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_local_only_commit_is_preserved_and_rejected(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        command("git", "add", "local.txt", cwd=self.repo)
        command("git", "commit", "-m", "local only", cwd=self.repo)
        local = command("git", "rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaisesRegex(lifecycle.LifecycleError, "local-only commits or divergence"):
            lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), local)

    def test_non_fast_forward_remote_tracking_movement_fails_closed(self) -> None:
        old = command("git", "rev-parse", "origin/main", cwd=self.repo)
        command("git", "checkout", "--orphan", "replacement", cwd=self.publisher)
        command("git", "rm", "-rf", ".", cwd=self.publisher)
        (self.publisher / "replacement.txt").write_text("replacement\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.publisher)
        command("git", "commit", "-m", "replacement", cwd=self.publisher)
        command("git", "push", "--force", "origin", "HEAD:main", cwd=self.publisher)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(command("git", "rev-parse", "origin/main", cwd=self.repo), old)

    def test_task_start_refreshes_a_later_remote_revision(self) -> None:
        expected = self.publish("later merge")
        worktree = self.start_task("TASK-2", "fresh")
        state = (worktree / ".task-state/task.md").read_text(encoding="utf-8")
        self.assertIn(f"- Base revision: {expected}", state)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=worktree), expected)

    def test_ambient_git_config_and_ssh_overrides_are_scrubbed(self) -> None:
        expected = self.publish("safe remote")
        marker = self.repo.parent / "ssh-command-ran"
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.url",
            "GIT_CONFIG_VALUE_0": "ssh://attacker.invalid/repository",
            "GIT_SSH_COMMAND": f"touch {marker}",
        }
        with mock.patch.dict(os.environ, injected):
            result = lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(result["revision"], expected)
        self.assertFalse(marker.exists())

    def test_github_default_branch_fallback_scrubs_repository_override(self) -> None:
        responses = [
            mock.Mock(returncode=1, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout='{"defaultBranchRef":{"name":"main"}}', stderr=""),
        ]
        with mock.patch.object(lifecycle, "run", side_effect=responses) as observed:
            self.assertEqual(lifecycle.default_branch(self.repo), "main")
        self.assertEqual(observed.call_args_list[1].kwargs["remove_env"], ("GH_REPO",))


class PostMergeFinalizationTest(RepositoryFixture):
    def merged_evidence(self, task_worktree: Path, merge_oid: str, **changes: object) -> dict:
        value = {
            "number": 93,
            "state": "MERGED",
            "headRefName": command("git", "branch", "--show-current", cwd=task_worktree),
            "baseRefName": "main",
            "isCrossRepository": False,
            "mergeCommit": {"oid": merge_oid},
        }
        value.update(changes)
        return value

    def prepare(self) -> tuple[Path, str, dict]:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("task change\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        merge_oid = self.publish("squash result")
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        return task_worktree, task_head, self.merged_evidence(task_worktree, merge_oid)

    def finalize(self, evidence: dict) -> None:
        with (
            mock.patch.object(agent_core, "pr_details", side_effect=[evidence, evidence]),
            mock.patch.object(
                agent_core,
                "prs_for_branch",
                return_value=[{"number": 93, "headRefName": evidence["headRefName"], "baseRefName": "main"}],
            ),
        ):
            agent_core.integrate_finalize(self.repo, "TASK-1", "93")

    def test_standard_squash_flow_and_idempotent_retry(self) -> None:
        task_worktree, task_head, evidence = self.prepare()
        self.finalize(evidence)
        self.assertEqual(lifecycle.state_status(task_worktree / ".task-state/task.md"), "merged")
        self.assertFalse(agent_core.merge_commit_is_ancestor(self.repo, task_head, evidence["mergeCommit"]["oid"]))
        self.finalize(evidence)
        self.assertEqual(lifecycle.state_status(task_worktree / ".task-state/task.md"), "merged")

    def test_finalized_task_is_eligible_for_existing_cleanup(self) -> None:
        task_worktree, _, evidence = self.prepare()
        branch = evidence["headRefName"]
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        self.finalize(evidence)
        real_run = lifecycle.run

        def fake_gh(command_args: list[str], **kwargs):
            if command_args[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(
                    command_args,
                    0,
                    json.dumps(
                        {
                            "state": "MERGED",
                            "headRefName": branch,
                            "headRefOid": command("git", "rev-parse", branch, cwd=self.repo),
                        }
                    ),
                    "",
                )
            return real_run(command_args, **kwargs)

        with mock.patch.object(lifecycle, "run", side_effect=fake_gh):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.repo,
        )
        self.assertNotEqual(branch_check.returncode, 0)

    def test_merge_commit_identity_is_accepted(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("merge style\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        command("git", "fetch", "origin", branch, cwd=self.publisher)
        command("git", "merge", "--no-ff", "FETCH_HEAD", "-m", "merge result", cwd=self.publisher)
        merge_oid = command("git", "rev-parse", "HEAD", cwd=self.publisher)
        self.assertEqual(command("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.publisher).count(" "), 2)
        command("git", "push", "origin", "main", cwd=self.publisher)
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        self.finalize(self.merged_evidence(task_worktree, merge_oid))
        self.assertEqual(lifecycle.state_status(state), "merged")

    def test_rebase_style_merge_identity_is_accepted_without_pr_head_ancestry(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("rebase style\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        self.publish("concurrent main advance")
        command("git", "fetch", "origin", branch, cwd=self.publisher)
        command("git", "cherry-pick", "FETCH_HEAD", cwd=self.publisher)
        merge_oid = command("git", "rev-parse", "HEAD", cwd=self.publisher)
        command("git", "push", "origin", "main", cwd=self.publisher)
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        self.finalize(self.merged_evidence(task_worktree, merge_oid))
        self.assertFalse(agent_core.merge_commit_is_ancestor(self.repo, task_head, merge_oid))
        self.assertEqual(lifecycle.state_status(state), "merged")

    def test_wrong_pr_evidence_and_ambiguity_are_rejected(self) -> None:
        task_worktree, _, evidence = self.prepare()
        invalid_values = (
            dict(evidence, state="OPEN"),
            dict(evidence, state="CLOSED"),
            dict(evidence, headRefName="task/OTHER-demo"),
            dict(evidence, baseRefName="release"),
            dict(evidence, number=94),
            dict(evidence, isCrossRepository=True),
            dict(evidence, mergeCommit=None),
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with mock.patch.object(agent_core, "pr_details", return_value=invalid):
                    with self.assertRaises(agent_core.AutomationError):
                        agent_core.merged_pr_evidence(self.repo, "TASK-1", "93")
        with (
            mock.patch.object(agent_core, "pr_details", return_value=evidence),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[]),
            self.assertRaisesRegex(agent_core.AutomationError, "missing or ambiguous"),
        ):
            agent_core.merged_pr_evidence(self.repo, "TASK-1", "93")

    def test_wrong_task_states_cannot_jump_to_merged(self) -> None:
        task_worktree = self.start_task()
        record = lifecycle.worktree_for_task(self.repo, "TASK-1")
        state = task_worktree / ".task-state/task.md"
        for status in ("implementing", "publication-ready", "draft-pr-created", "blocked"):
            with self.subTest(status=status):
                text = state.read_text(encoding="utf-8")
                text = text[: text.index("- Status:")] + f"- Status: {status}\n"
                state.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(lifecycle.LifecycleError, "requires Task status"):
                    lifecycle.mark_task_merged_from_integration(record, "TASK-1")

    def test_merge_result_must_be_in_synchronized_default_branch(self) -> None:
        _, _, evidence = self.prepare()
        evidence = dict(evidence, mergeCommit={"oid": "a" * 40})
        with (
            mock.patch.object(agent_core, "pr_details", return_value=evidence),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[{"number": 93, "headRefName": evidence["headRefName"]}]),
            self.assertRaisesRegex(agent_core.AutomationError, "not present"),
        ):
            agent_core.integrate_finalize(self.repo, "TASK-1", "93")

    def test_ref_movement_after_evidence_revalidation_does_not_mark_merged(self) -> None:
        task_worktree, _, evidence = self.prepare()
        with (
            mock.patch.object(agent_core, "pr_details", side_effect=[evidence, evidence]),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[{"number": 93, "headRefName": evidence["headRefName"]}]),
            mock.patch.object(
                lifecycle,
                "require_synchronized_default_branch_revision",
                side_effect=lifecycle.LifecycleError("refs moved"),
            ),
            self.assertRaisesRegex(agent_core.AutomationError, "refs moved"),
        ):
            agent_core.integrate_finalize(self.repo, "TASK-1", "93")
        self.assertEqual(
            lifecycle.state_status(task_worktree / ".task-state/task.md"),
            "integration-pending",
        )

    def test_just_exposes_finalize_without_raw_git(self) -> None:
        recipe = (ROOT / "components/agent-core/.automation/just/integrate.just").read_text(encoding="utf-8")
        self.assertIn("finalize task pr:", recipe)
        self.assertIn("integrate finalize", recipe)
        self.assertNotIn("git fetch", recipe)


if __name__ == "__main__":
    unittest.main()
