from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "agent-base"


def run(
    *args: str,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check, env=env)


class WorktreeLifecycleSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        temporary_root = Path(self.temporary.name)
        self.repo = temporary_root / "repo"
        shutil.copytree(TEMPLATE, self.repo)
        run("git", "init", "-b", "main", cwd=self.repo)
        run("git", "config", "user.email", "smoke@example.invalid", cwd=self.repo)
        run("git", "config", "user.name", "Smoke", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "initial", cwd=self.repo)
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        run("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        self.script = self.repo / ".automation" / "bin" / "task_lifecycle.py"

        fake_bin = temporary_root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_gh.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{fake_bin}:{self.env['PATH']}"

    def test_start_duplicate_ignore_and_cleanup(self) -> None:
        first = run("python3", str(self.script), "start", "TASK-1", "smoke", cwd=self.repo, env=self.env)
        self.assertIn('"status": "initialized"', first.stdout)
        worktree = self.repo / ".worktrees" / "TASK-1-smoke"
        state = worktree / ".task-state" / "task.md"
        self.assertTrue(state.is_file())
        status = run("git", "status", "--porcelain", cwd=worktree).stdout
        self.assertNotIn(".task-state", status)

        duplicate = run(
            "python3",
            str(self.script),
            "start",
            "TASK-1",
            "smoke",
            cwd=self.repo,
            check=False,
            env=self.env,
        )
        self.assertNotEqual(duplicate.returncode, 0)

        task_policy = worktree / ".automation" / "model-fallback.toml"
        original_task_policy_text = task_policy.read_text(encoding="utf-8")
        task_policy_text = original_task_policy_text.replace(
            'fallback_models = ["openai/gpt-5.6-luna"]',
            'fallback_models = ["openai/gpt-5.6-terra"]',
            1,
        )
        task_policy.write_text(task_policy_text, encoding="utf-8")

        mismatch = run(
            "python3", str(self.script), "recovery-start", "TASK-1", "spark",
            cwd=self.repo, check=False, env=self.env,
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("agent model mismatch for general-fallback", mismatch.stderr)

        unknown_task_policy = task_policy_text.replace(
            'spark = ["openai/gpt-5.3-codex-spark"]',
            'task-spark = ["openai/gpt-5.3-codex-spark"]',
        )
        task_policy.write_text(unknown_task_policy, encoding="utf-8")
        unknown = run(
            "python3", str(self.script), "recovery-start", "TASK-1", "spark",
            cwd=self.repo, check=False, env=self.env,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown model family", unknown.stderr)
        task_policy.write_text(task_policy_text, encoding="utf-8")

        task_agent = worktree / ".opencode" / "agents" / "general-fallback.md"
        main_agent = self.repo / ".opencode" / "agents" / "general-fallback.md"
        original_task_agent_text = task_agent.read_text(encoding="utf-8")
        original_main_agent_text = main_agent.read_text(encoding="utf-8")
        task_agent.write_text(
            original_task_agent_text.replace("model: openai/gpt-5.6-luna", "model: openai/gpt-5.6-terra"),
            encoding="utf-8",
        )
        main_mismatch = run(
            "python3", str(self.script), "recovery-start", "TASK-1", "spark",
            cwd=self.repo, check=False, env=self.env,
        )
        self.assertNotEqual(main_mismatch.returncode, 0)
        self.assertIn(f"root={self.repo}", main_mismatch.stderr)
        main_agent.write_text(
            original_main_agent_text.replace("model: openai/gpt-5.6-luna", "model: openai/gpt-5.6-terra"),
            encoding="utf-8",
        )
        registered = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-1", "general",
            "Implement the bounded recovery fixture", cwd=worktree, env=self.env,
        )
        work_unit = json.loads(registered.stdout)
        self.assertEqual(work_unit["state"], "in-flight")
        failed_unit = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "failed",
            cwd=worktree, env=self.env,
        )
        self.assertEqual(json.loads(failed_unit.stdout)["state"], "failed")

        started = run(
            "python3", str(self.script), "recovery-start", "TASK-1", "spark", cwd=self.repo, env=self.env
        )
        recovery = json.loads(started.stdout)
        self.assertEqual(recovery["routing"]["task-orchestrator"], "task-orchestrator-fallback")
        self.assertEqual(recovery["routing"]["general"], "general-fallback")
        self.assertEqual(recovery["routes"]["general"]["model"], "openai/gpt-5.6-terra")
        self.assertEqual(recovery["recoverable_work_units"][0]["id"], "WU-1")
        self.assertEqual(recovery["recoverable_work_units"][0]["objective"], "Implement the bounded recovery fixture")
        late_unit = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-LATE", "general",
            "A new unit created after recovery start", cwd=worktree, env=self.env,
        )
        late_digest = json.loads(late_unit.stdout)["semantic_sha256"]
        late_record = run(
            "python3", str(self.script), "recovery-record", "TASK-1", "general", "WU-LATE",
            late_digest, "completed", cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(late_record.returncode, 0)
        self.assertIn("not recoverable when recovery started", late_record.stderr)
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("operator-asserted usage-limit observation (runtime unverified)", state_text)
        self.assertNotIn("reason=genuine usage-limit observation", state_text)
        main_route = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=self.repo, env=self.env
        )
        task_route = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=worktree, env=self.env
        )
        self.assertEqual(json.loads(main_route.stdout)["selected"], "general-fallback")
        self.assertEqual(json.loads(main_route.stdout)["model"], "openai/gpt-5.6-terra")
        self.assertEqual(json.loads(task_route.stdout), json.loads(main_route.stdout))
        recovery_path = worktree / ".task-state" / "recovery.json"
        tampered = json.loads(recovery_path.read_text(encoding="utf-8"))
        tampered["routes"]["general"]["agent"] = "general"
        recovery_path.write_text(json.dumps(tampered), encoding="utf-8")
        route = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=self.repo, env=self.env
        )
        self.assertEqual(json.loads(route.stdout)["selected"], "general-fallback")
        self.assertEqual(json.loads(route.stdout)["model"], "openai/gpt-5.6-terra")
        unknown_unit = run(
            "python3", str(self.script), "recovery-record", "TASK-1", "general", "WU-2",
            work_unit["semantic_sha256"], "completed", cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(unknown_unit.returncode, 0)
        self.assertIn("unknown Work Unit", unknown_unit.stderr)
        semantic_mismatch = run(
            "python3", str(self.script), "recovery-record", "TASK-1", "general", "WU-1",
            "0" * 64, "completed", cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(semantic_mismatch.returncode, 0)
        self.assertIn("semantic mismatch", semantic_mismatch.stderr)
        recorded = run(
            "python3", str(self.script), "recovery-record", "TASK-1", "general", "WU-1",
            work_unit["semantic_sha256"], "completed",
            cwd=worktree, env=self.env,
        )
        self.assertEqual(json.loads(recorded.stdout)["selected_agent"], "general-fallback")
        self.assertEqual(json.loads(recorded.stdout)["selected_model"], "openai/gpt-5.6-terra")
        persisted_unit = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-1", cwd=worktree, env=self.env
        )
        self.assertEqual(json.loads(persisted_unit.stdout)["state"], "completed")
        reopen = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "in-flight",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(reopen.returncode, 0)
        self.assertIn("invalid Work Unit transition", reopen.stderr)

        exhausted_policy = task_policy.read_text(encoding="utf-8").replace(
            'fallback_agents = ["general-fallback"]',
            "fallback_agents = []",
        ).replace(
            'fallback_models = ["openai/gpt-5.6-terra"]',
            "fallback_models = []",
            1,
        )
        task_policy.write_text(exhausted_policy, encoding="utf-8")
        exhausted = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=self.repo, env=self.env
        )
        self.assertEqual(json.loads(exhausted.stdout)["status"], "BLOCKED")
        task_policy.write_text(original_task_policy_text, encoding="utf-8")
        task_agent.write_text(original_task_agent_text, encoding="utf-8")
        main_agent.write_text(original_main_agent_text, encoding="utf-8")
        run("python3", str(self.script), "recovery-clear", "TASK-1", cwd=self.repo, env=self.env)
        self.assertFalse((worktree / ".task-state" / "recovery.json").exists())

        run("python3", str(self.script), "state-set", "TASK-1", "cancelled", cwd=worktree, env=self.env)
        cleanup = run("python3", str(self.script), "cleanup", "TASK-1", cwd=self.repo, env=self.env)
        self.assertIn('"taskStateDiscarded": true', cleanup.stdout)
        self.assertFalse(worktree.exists())

if __name__ == "__main__":
    unittest.main()
