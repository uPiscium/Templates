from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "agent-base"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


class WorktreeLifecycleSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(TEMPLATE, self.repo)
        run("git", "init", "-b", "main", cwd=self.repo)
        run("git", "config", "user.email", "smoke@example.invalid", cwd=self.repo)
        run("git", "config", "user.name", "Smoke", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "initial", cwd=self.repo)
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        run("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        self.script = self.repo / ".automation" / "bin" / "task_lifecycle.py"

    def test_start_duplicate_ignore_and_cleanup(self) -> None:
        first = run("python3", str(self.script), "start", "TASK-1", "smoke", cwd=self.repo)
        self.assertIn('"status": "initialized"', first.stdout)
        worktree = self.repo / ".worktrees" / "TASK-1-smoke"
        state = worktree / ".task-state" / "task.md"
        self.assertTrue(state.is_file())
        status = run("git", "status", "--porcelain", cwd=worktree).stdout
        self.assertNotIn(".task-state", status)

        duplicate = run("python3", str(self.script), "start", "TASK-1", "smoke", cwd=self.repo, check=False)
        self.assertNotEqual(duplicate.returncode, 0)

        run("python3", str(self.script), "state-set", "TASK-1", "cancelled", cwd=worktree)
        cleanup = run("python3", str(self.script), "cleanup", "TASK-1", cwd=self.repo)
        self.assertIn('"taskStateDiscarded": true', cleanup.stdout)
        self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
