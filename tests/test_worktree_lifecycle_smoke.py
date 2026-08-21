from __future__ import annotations

import hashlib
import json
import os
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

    def test_start_work_units_duplicate_and_cleanup(self) -> None:
        first = run("python3", str(self.script), "start", "TASK-1", "smoke", cwd=self.repo, env=self.env)
        self.assertIn('"status": "initialized"', first.stdout)
        worktree = self.repo / ".worktrees" / "TASK-1-smoke"
        state = worktree / ".task-state" / "task.md"
        self.assertTrue(state.is_file())
        self.assertNotIn(".task-state", run("git", "status", "--porcelain", cwd=worktree).stdout)

        duplicate_task = run(
            "python3", str(self.script), "start", "TASK-1", "smoke",
            cwd=self.repo, check=False, env=self.env,
        )
        self.assertNotEqual(duplicate_task.returncode, 0)

        objective = "Implement the bounded fixed-model fixture"
        registered = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-1", "general", objective,
            cwd=worktree, env=self.env,
        )
        unit = json.loads(registered.stdout)
        self.assertEqual("in-flight", unit["state"])
        self.assertEqual(objective, unit["objective"])
        self.assertEqual(hashlib.sha256(objective.encode()).hexdigest(), unit["semantic_sha256"])

        persisted = json.loads((worktree / ".task-state" / "work-units.json").read_text(encoding="utf-8"))
        self.assertEqual("TASK-1", persisted["task_id"])
        self.assertEqual(unit, persisted["units"]["WU-1"])
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("## Work Units", state_text)
        self.assertNotIn("## Work Units\n\nNone yet.", state_text)
        self.assertIn("work_unit_registered=WU-1", state_text)
        self.assertIn(f"semantic_sha256={unit['semantic_sha256']}", state_text)

        duplicate_unit = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-1", "general", objective,
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(duplicate_unit.returncode, 0)
        self.assertIn("Work Unit already exists", duplicate_unit.stderr)

        status = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-1",
            cwd=worktree, env=self.env,
        )
        self.assertEqual(unit, json.loads(status.stdout))

        completed = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "completed",
            "Leaf returned COMPLETED after implementing the bounded fixture",
            cwd=worktree, env=self.env,
        )
        completed_unit = json.loads(completed.stdout)
        self.assertEqual("completed", completed_unit["state"])
        self.assertEqual(
            "Leaf returned COMPLETED after implementing the bounded fixture",
            completed_unit["transitions"][0]["evidence"],
        )
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("work_unit_state=WU-1; previous=in-flight; state=completed", state_text)
        self.assertIn("evidence_sha256=", state_text)

        reopen = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "in-flight",
            "Attempt to reopen a terminal unit",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(reopen.returncode, 0)
        self.assertIn("invalid Work Unit transition", reopen.stderr)

        unknown = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-UNKNOWN",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown Work Unit", unknown.stderr)

        blocked = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-2", "verifier",
            "Run the bounded fixed-model verification", cwd=worktree, env=self.env,
        )
        self.assertEqual("in-flight", json.loads(blocked.stdout)["state"])
        blocked = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-2", "blocked",
            "Configured verifier model was unavailable",
            "--provider", "openai", "--model", "gpt-5.6-luna", "--error", "model unavailable",
            cwd=worktree, env=self.env,
        )
        failure = json.loads(blocked.stdout)["transitions"][0]["provider_failure"]
        self.assertEqual(
            {"provider": "openai", "model": "gpt-5.6-luna", "error": "model unavailable"},
            failure,
        )

        run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-3", "general",
            "Reject contradictory completion evidence", cwd=worktree, env=self.env,
        )
        contradictory = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-3", "completed",
            "Contradictory provider failure",
            "--provider", "openai", "--model", "gpt-5.6-luna", "--error", "model unavailable",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(contradictory.returncode, 0)
        self.assertIn("only valid for a blocked Work Unit", contradictory.stderr)
        unchanged = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-3",
            cwd=worktree, env=self.env,
        )
        self.assertEqual("in-flight", json.loads(unchanged.stdout)["state"])

        run("python3", str(self.script), "state-set", "TASK-1", "cancelled", cwd=worktree, env=self.env)
        cleanup = run("python3", str(self.script), "cleanup", "TASK-1", cwd=self.repo, env=self.env)
        self.assertIn('"taskStateDiscarded": true', cleanup.stdout)
        self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
