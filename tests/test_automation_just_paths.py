from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_MODULE = ".automation/just/automation.just"
TEMPLATE_NAMES = ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake")
RECIPES = (
    ("automation::version",),
    ("automation::check-update", "."),
    ("automation::upgrade", "."),
    ("automation::bootstrap-receipt", "."),
    ("automation::commit", "TASK-81", "message"),
)


class AutomationJustPathTest(unittest.TestCase):
    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)

    @staticmethod
    def _dry_run(repo: Path, *recipe: str) -> str:
        result = subprocess.run(
            ("just", "--dry-run", "--justfile", str(repo / "Justfile"), *recipe),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    @staticmethod
    def _resolved_scripts(output: str) -> list[Path]:
        """Read script arguments from Just's shell-quoted dry-run commands."""
        scripts: list[Path] = []
        for line in output.splitlines():
            for token in shlex.split(line):
                if token.endswith("automation_upgrade.py"):
                    scripts.append(Path(token).expanduser().resolve())
        return scripts

    def test_source_and_generated_automation_modules_use_their_own_root(self) -> None:
        source = ROOT / "components" / "agent-core" / AUTOMATION_MODULE
        self.assertEqual(source.read_text(encoding="utf-8").splitlines()[0], "root := justfile_directory()")
        self.assertNotIn("../..", source.read_text(encoding="utf-8"))

        for name in TEMPLATE_NAMES:
            module_dir = ROOT / "templates" / name / ".automation" / "just"
            for path in sorted(module_dir.glob("*.just")):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[0], "root := justfile_directory()", path)
                self.assertNotIn("../..", text, path)

    def test_agent_core_just_modules_keep_justfile_directory_root(self) -> None:
        module_dir = ROOT / "components" / "agent-core" / ".automation" / "just"
        for path in sorted(module_dir.glob("*.just")):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.splitlines()[0], "root := justfile_directory()", path)
            self.assertNotIn("../..", text, path)

    def test_dry_runs_select_main_repository_script(self) -> None:
        self._assert_dry_run_selects_local_script(linked=False)

    def test_dry_runs_select_linked_worktree_script(self) -> None:
        self._assert_dry_run_selects_local_script(linked=True)

    def _assert_dry_run_selects_local_script(self, *, linked: bool) -> None:
        if shutil.which("just") is None:
            self.skipTest("Just executable is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory) / "nested" / "main-repository"
            outer.mkdir(parents=True)
            shutil.copytree(ROOT / "templates" / "agent-base", outer, symlinks=True, dirs_exist_ok=True)
            self._git(outer, "init", "-b", "main")
            self._git(outer, "config", "user.name", "Automation Just Test")
            self._git(outer, "config", "user.email", "automation-just@example.invalid")
            self._git(outer, "add", "-A")
            self._git(outer, "commit", "-m", "agent-base fixture")

            decoy_paths = (
                outer.parent / ".automation" / "bin" / "automation_upgrade.py",
                outer.parent.parent / ".automation" / "bin" / "automation_upgrade.py",
            )
            for decoy in decoy_paths:
                decoy.parent.mkdir(parents=True, exist_ok=True)
                decoy.write_text("decoy\n", encoding="utf-8")

            target = outer
            if linked:
                target = outer / ".worktrees" / "linked-task"
                self._git(outer, "worktree", "add", "-b", "task/81-paths", str(target))

            expected = (target / ".automation" / "bin" / "automation_upgrade.py").resolve()
            forbidden = {
                (outer / ".automation" / "bin" / "automation_upgrade.py").resolve(),
                *(path.resolve() for path in decoy_paths),
            }
            for recipe in RECIPES:
                scripts = self._resolved_scripts(self._dry_run(target, *recipe))
                self.assertEqual(scripts, [expected], recipe)
                self.assertTrue(scripts[0].is_relative_to(target.resolve()), recipe)
                disallowed = forbidden if linked else {path.resolve() for path in decoy_paths}
                self.assertNotIn(scripts[0], disallowed, recipe)


if __name__ == "__main__":
    unittest.main()
