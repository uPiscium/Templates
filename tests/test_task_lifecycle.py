from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py"
spec = importlib.util.spec_from_file_location("task_lifecycle", MODULE_PATH)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lifecycle
spec.loader.exec_module(lifecycle)


class TaskLifecycleTest(unittest.TestCase):
    def test_task_branch_matching_is_not_substring_based(self) -> None:
        self.assertTrue(lifecycle.branch_matches_task("task/TASK-1-example", "TASK-1"))
        self.assertFalse(lifecycle.branch_matches_task("task/TASK-10-example", "TASK-1"))

    def test_state_transition_rejects_invalid_jump(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("## Current state\n\n- Status: initialized\n", encoding="utf-8")
            with self.assertRaisesRegex(lifecycle.LifecycleError, "invalid Task State transition"):
                lifecycle.set_state_status(path, "merged")
            lifecycle.set_state_status(path, "planning")
            self.assertEqual(lifecycle.state_status(path), "planning")

    def test_batch_conflict_detects_dependency_and_shared_resources(self) -> None:
        summaries = [
            {
                "task": "TASK-1",
                "dependencies": [],
                "scope": ["src/a.cpp"],
                "coordinationSurfaces": ["flake.lock"],
                "externalResources": ["test-db"],
            },
            {
                "task": "TASK-2",
                "dependencies": ["TASK-1"],
                "scope": ["src/b.cpp"],
                "coordinationSurfaces": ["flake.lock"],
                "externalResources": ["test-db"],
            },
        ]
        conflicts = lifecycle.batch_conflicts(summaries)
        self.assertEqual(len(conflicts), 1)
        reasons = conflicts[0]["reasons"]
        self.assertIn("declared dependency", reasons)
        self.assertTrue(any("coordination surface" in reason for reason in reasons))
        self.assertTrue(any("external resource" in reason for reason in reasons))

    def test_next_work_unit_uses_max_canonical_suffix_without_filling_gaps(self) -> None:
        value = {
            "units": {
                "WU-TASK-1-01": {},
                "WU-TASK-1-03": {},
                "WU-TASK-1-003": {},
                "WU-TASK-10-99": {},
                "legacy-unit": {},
            }
        }
        self.assertEqual("WU-TASK-1-04", lifecycle.next_work_unit_id(value, "TASK-1"))
        self.assertEqual(3, lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-1-03"))
        self.assertIsNone(
            lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-1-003")
        )
        self.assertIsNone(
            lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-10-99")
        )

    def test_next_work_unit_rejects_oversized_generated_id(self) -> None:
        task = "T" * 124
        self.assertTrue(lifecycle.TASK_RE.fullmatch(task))
        with self.assertRaisesRegex(
            lifecycle.LifecycleError, "generated Work Unit ID is invalid"
        ):
            lifecycle.next_work_unit_id({"units": {}}, task)

    def test_generated_lifecycle_files_match_sources(self) -> None:
        pairs = [
            (
                ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py",
                ROOT / "templates" / "agent-base" / ".automation" / "bin" / "task_lifecycle.py",
            ),
            (
                ROOT / "components" / "agent-core" / ".automation" / "just" / "agent.just",
                ROOT / "templates" / "agent-base" / ".automation" / "just" / "agent.just",
            ),
            (
                ROOT / "components" / "agent-core" / ".automation" / "templates" / "task-state.md",
                ROOT / "templates" / "agent-base" / ".automation" / "templates" / "task-state.md",
            ),
            (
                ROOT / "components" / "agent-core" / "opencode.json",
                ROOT / "templates" / "agent-base" / "opencode.json",
            ),
        ]
        for source, generated in pairs:
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())


if __name__ == "__main__":
    unittest.main()
