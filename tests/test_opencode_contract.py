from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core"
AGENTS = CORE / ".opencode" / "agents"


def frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    return match.group(1)


class OpenCodeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((CORE / "opencode.json").read_text(encoding="utf-8"))

    def test_depth_and_default_agent(self) -> None:
        self.assertEqual(self.config["default_agent"], "build")
        self.assertEqual(self.config["subagent_depth"], 2)

    def test_external_directory_boundary(self) -> None:
        rules = self.config["permission"]["external_directory"]
        self.assertEqual(rules["*"], "deny")
        self.assertEqual(rules["/tmp/opencode"], "ask")
        self.assertEqual(rules["/tmp/opencode/**"], "ask")

    def test_git_and_github_writes_require_just_api(self) -> None:
        bash = self.config["permission"]["bash"]
        self.assertEqual(bash["git push *"], "deny")
        self.assertEqual(bash["git commit *"], "deny")
        self.assertEqual(bash["gh pr create *"], "deny")
        self.assertEqual(bash["gh pr edit *"], "deny")
        self.assertEqual(bash["gh pr ready *"], "deny")
        self.assertEqual(bash["gh pr merge *"], "deny")
        self.assertEqual(bash["just agent::commit *"], "allow")
        self.assertEqual(bash["just agent::pr-create *"], "allow")
        self.assertEqual(bash["just agent::pr-edit *"], "allow")
        self.assertEqual(bash["just agent::pr-ready *"], "allow")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["just integrate::merge *"], "ask")

    def test_automation_core_is_not_editable(self) -> None:
        edit = self.config["permission"]["edit"]
        for path in (
            "opencode.json",
            "AGENTS.md",
            "Justfile",
            ".opencode/**",
            ".automation/**",
            ".github/workflows/**",
            ".task-state/**",
        ):
            self.assertEqual(edit[path], "deny", path)

    def test_model_assignment_is_exact(self) -> None:
        expected = {
            "build.md": "openai/gpt-5.6-sol",
            "task-orchestrator.md": "openai/gpt-5.3-codex-spark",
            "general.md": "openai/gpt-5.6-luna",
            "explore.md": "openai/gpt-5.6-luna",
            "verifier.md": "openai/gpt-5.6-luna",
            "reviewer.md": "openai/gpt-5.6-terra",
            "investigator.md": "openai/gpt-5.6-terra",
            "security-reviewer.md": "openai/gpt-5.6-terra",
            "scout.md": "openai/gpt-5.6-luna",
            "architect.md": "openai/gpt-5.6-sol",
        }
        for filename, model in expected.items():
            self.assertIn(f"model: {model}", frontmatter(AGENTS / filename), filename)

    def test_task_orchestrator_is_only_spark_primary(self) -> None:
        spark_primaries = {
            path.name
            for path in AGENTS.glob("*.md")
            if not path.name.endswith("-fallback.md")
            and "model: openai/gpt-5.3-codex-spark" in frontmatter(path)
        }
        self.assertEqual(spark_primaries, {"task-orchestrator.md"})

    def test_luna_primary_agents(self) -> None:
        expected_luna = {
            "general.md",
            "explore.md",
            "verifier.md",
            "scout.md",
        }
        for filename in expected_luna:
            self.assertIn("model: openai/gpt-5.6-luna", frontmatter(AGENTS / filename), filename)

    def test_fallback_model_assignment_is_exact(self) -> None:
        expected = {
            "build-fallback.md": "openai/gpt-5.6-terra",
            "architect-fallback.md": "openai/gpt-5.6-terra",
            "task-orchestrator-fallback.md": "openai/gpt-5.6-sol",
            "general-fallback.md": "openai/gpt-5.3-codex-spark",
            "explore-fallback.md": "openai/gpt-5.3-codex-spark",
            "verifier-fallback.md": "openai/gpt-5.3-codex-spark",
            "reviewer-fallback.md": "openai/gpt-5.6-sol",
            "investigator-fallback.md": "openai/gpt-5.6-sol",
            "security-reviewer-fallback.md": "openai/gpt-5.6-sol",
            "scout-fallback.md": "openai/gpt-5.6-terra",
        }
        for filename, model in expected.items():
            self.assertIn(f"model: {model}", frontmatter(AGENTS / filename), filename)

    def test_leaf_agents_cannot_delegate(self) -> None:
        leaves = (
            "general.md",
            "explore.md",
            "verifier.md",
            "reviewer.md",
            "investigator.md",
            "security-reviewer.md",
            "scout.md",
            "architect.md",
        )
        for filename in leaves:
            self.assertIn("task: deny", frontmatter(AGENTS / filename), filename)

    def test_task_orchestrator_call_graph_is_non_cyclic(self) -> None:
        fm = frontmatter(AGENTS / "task-orchestrator.md")
        task_section = fm.split("permission:\n", 1)[1].split("  bash:\n", 1)[0]
        self.assertIn('"*": deny', task_section)
        self.assertNotIn("task-orchestrator: allow", task_section)
        for leaf in ("general", "explore", "verifier", "reviewer", "investigator", "security-reviewer", "scout"):
            self.assertIn(f"{leaf}: allow", task_section)


if __name__ == "__main__":
    unittest.main()
