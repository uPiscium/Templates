from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core" / ".opencode"

TASK_EDGE_RE = re.compile(r"^\s{4}([a-z0-9-]+): allow\s*$", re.MULTILINE)


class OpenCodeDiscoverySmokeTest(unittest.TestCase):
    def test_required_commands_and_skills_are_discoverable(self) -> None:
        for relative in (
            "commands/init.md",
            "commands/task-start.md",
            "commands/task-run.md",
            "commands/task-batch.md",
            "skills/initialize/SKILL.md",
            "skills/task-orchestration/SKILL.md",
        ):
            self.assertTrue((CORE / relative).is_file(), relative)

    def test_primary_call_graph_is_acyclic(self) -> None:
        agents = CORE / "agents"
        graph: dict[str, set[str]] = {}
        for path in agents.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            graph[path.stem] = set(TASK_EDGE_RE.findall(text))

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                self.fail(f"cyclic task graph at {node}")
            if node in visited:
                return
            visiting.add(node)
            for child in graph.get(node, set()):
                if child in graph:
                    visit(child)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    def test_leaf_agents_cannot_redelegate(self) -> None:
        leaves = (
            "general",
            "explore",
            "verifier",
            "reviewer",
            "investigator",
            "security-reviewer",
            "scout",
            "architect",
        )
        for leaf in leaves:
            for suffix in ("", "-fallback"):
                path = CORE / "agents" / f"{leaf}{suffix}.md"
                if not path.exists():
                    continue
                text = path.read_text(encoding="utf-8")
                self.assertRegex(text, r"(?m)^\s{2}task:\s*deny\s*$", path.name)

    def test_task_orchestrator_cannot_call_itself(self) -> None:
        text = (CORE / "agents" / "task-orchestrator.md").read_text(encoding="utf-8")
        edges = set(TASK_EDGE_RE.findall(text))
        self.assertNotIn("task-orchestrator", edges)
        self.assertNotIn("task-orchestrator-fallback", edges)


if __name__ == "__main__":
    unittest.main()
