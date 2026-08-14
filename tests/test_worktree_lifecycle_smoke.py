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

        started = run(
            "python3", str(self.script), "recovery-start", "TASK-1", "spark", cwd=self.repo, env=self.env
        )
        recovery = json.loads(started.stdout)
        self.assertEqual(recovery["routing"]["task-orchestrator"], "task-orchestrator-fallback")
        self.assertEqual(recovery["routing"]["general"], "general-fallback")
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("operator-asserted usage-limit observation (runtime unverified)", state_text)
        self.assertNotIn("reason=genuine usage-limit observation", state_text)
        route = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=self.repo, env=self.env
        )
        self.assertEqual(json.loads(route.stdout)["selected"], "general-fallback")
        recovery_path = worktree / ".task-state" / "recovery.json"
        tampered = json.loads(recovery_path.read_text(encoding="utf-8"))
        tampered["routes"]["general"]["agent"] = "general"
        recovery_path.write_text(json.dumps(tampered), encoding="utf-8")
        route = run(
            "python3", str(self.script), "recovery-route", "TASK-1", "general", cwd=self.repo, env=self.env
        )
        self.assertEqual(json.loads(route.stdout)["selected"], "general-fallback")
        run(
            "python3", str(self.script), "recovery-record", "TASK-1", "general", "WU-1", "completed",
            cwd=worktree, env=self.env,
        )
        run("python3", str(self.script), "recovery-clear", "TASK-1", cwd=self.repo, env=self.env)
        self.assertFalse((worktree / ".task-state" / "recovery.json").exists())

        run("python3", str(self.script), "state-set", "TASK-1", "cancelled", cwd=worktree, env=self.env)
        cleanup = run("python3", str(self.script), "cleanup", "TASK-1", cwd=self.repo, env=self.env)
        self.assertIn('"taskStateDiscarded": true', cleanup.stdout)
        self.assertFalse(worktree.exists())

if __name__ == "__main__":
    unittest.main()
