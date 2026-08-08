from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import adopt_repository  # noqa: E402


class AdoptRepositoryTest(unittest.TestCase):
    def make_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        return temporary, root

    def commit_all(self, root: Path, message: str = "initial") -> str:
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip()

    def test_plan_is_read_only_and_auto_falls_back_to_base_without_marker(self) -> None:
        temporary, repo = self.make_repo()
        self.addCleanup(temporary.cleanup)
        (repo / ".keep").write_text("tracked\n", encoding="utf-8")
        self.commit_all(repo)
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout

        plan = adopt_repository.build_plan(ROOT, repo, "auto")

        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout
        self.assertEqual(plan["selectedAdapter"], "base")
        self.assertIn("base fallback", plan["adapterSelectionReason"])
        self.assertEqual(before, after)
        self.assertFalse(plan["workingTreeDirty"])

    def test_auto_detects_nix_when_flake_is_only_project_marker(self) -> None:
        temporary, repo = self.make_repo()
        self.addCleanup(temporary.cleanup)
        (repo / "flake.nix").write_text("{ outputs = { self }: {}; }\n", encoding="utf-8")
        self.commit_all(repo)

        plan = adopt_repository.build_plan(ROOT, repo, "auto")

        self.assertEqual(plan["selectedAdapter"], "nix")
        self.assertIn("flake.nix", plan["adapterSelectionReason"])

    def test_base_apply_preserves_repository_files_and_does_not_commit(self) -> None:
        temporary, repo = self.make_repo()
        self.addCleanup(temporary.cleanup)
        original_flake = "{ outputs = { self }: { existing = true; }; }\n"
        (repo / "flake.nix").write_text(original_flake, encoding="utf-8")
        (repo / "Justfile").write_text("default:\n    @echo existing\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text("# Existing Repository Rules\n", encoding="utf-8")
        (repo / ".gitignore").write_text("result\n", encoding="utf-8")
        head_before = self.commit_all(repo)

        plan = adopt_repository.build_plan(ROOT, repo, "base")
        self.assertTrue(plan["canApply"], plan["blockers"])
        result = adopt_repository.apply_plan(ROOT, repo, "base")

        self.assertTrue(result["applied"])
        self.assertEqual((repo / "flake.nix").read_text(encoding="utf-8"), original_flake)
        justfile = (repo / "Justfile").read_text(encoding="utf-8")
        self.assertIn("default:", justfile)
        self.assertIn("mod agent '.automation/just/agent.just'", justfile)
        self.assertIn("mod project 'just/project/mod.just'", justfile)
        agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# Existing Repository Rules", agents)
        self.assertIn("<!-- BEGIN AGENT CORE RULES -->", agents)
        self.assertIn("/.worktrees/", (repo / ".gitignore").read_text(encoding="utf-8"))
        self.assertEqual((repo / ".automation" / "ADAPTER").read_text().strip(), "base")
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(head_before, head_after)
        self.assertFalse(result["commitCreated"])
        self.assertFalse(result["pushPerformed"])
        self.assertFalse(result["mergePerformed"])

    def test_existing_opencode_collision_blocks_apply(self) -> None:
        temporary, repo = self.make_repo()
        self.addCleanup(temporary.cleanup)
        (repo / "opencode.json").write_text('{"existing": true}\n', encoding="utf-8")
        self.commit_all(repo)

        plan = adopt_repository.build_plan(ROOT, repo, "base")

        self.assertFalse(plan["canApply"])
        self.assertTrue(any("opencode.json" in blocker for blocker in plan["blockers"]))

    def test_dirty_repository_cannot_apply(self) -> None:
        temporary, repo = self.make_repo()
        self.addCleanup(temporary.cleanup)
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        self.commit_all(repo)
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")

        plan = adopt_repository.build_plan(ROOT, repo, "base")

        self.assertTrue(plan["workingTreeDirty"])
        self.assertFalse(plan["canApply"])
        with self.assertRaisesRegex(adopt_repository.AdoptionError, "working tree is dirty"):
            adopt_repository.apply_plan(ROOT, repo, "base")

    def test_conflicting_just_module_blocks_safe_merge(self) -> None:
        existing = "mod agent 'custom/agent.just'\n"
        merged, reason = adopt_repository.just_router_merge(existing)
        self.assertIsNone(merged)
        self.assertIn("conflicts", reason)


if __name__ == "__main__":
    unittest.main()
