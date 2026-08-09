from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "runtime" / "runtime_smoke.py"
SPEC = importlib.util.spec_from_file_location("runtime_smoke", RUNNER)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSmokeHarnessTest(unittest.TestCase):
    def test_runtime_workspace_is_git_ignored_and_routed_through_just(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.runtime-smoke/", ignore)
        justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
        self.assertIn("mod runtime 'just/runtime.just'", justfile)
        module = (ROOT / "just" / "runtime.just").read_text(encoding="utf-8")
        for recipe in (
            "prepare issue='issue-41' template='agent-python'",
            "diagnose-child-stall issue='issue-41'",
            "smoke-depth2 issue='issue-41'",
            "smoke-fallback issue='issue-41'",
            "direct-leaf issue='issue-41'",
            "export-session session issue='issue-41'",
        ):
            self.assertIn(recipe, module)

    def test_workspace_names_cannot_escape_runtime_root(self) -> None:
        with self.assertRaises(runtime.RuntimeSmokeError):
            runtime.workspace("../outside")
        with self.assertRaises(runtime.RuntimeSmokeError):
            runtime.validate_name("bad/name", "name")
        resolved = runtime.workspace("issue-41")
        self.assertEqual(resolved.parent, ROOT / ".runtime-smoke")

    def test_deterministic_runtime_tasks_are_defined(self) -> None:
        tasks = {(task, slug) for task, slug, *_ in runtime.TASK_DEFINITIONS}
        self.assertEqual(
            tasks,
            {
                ("SMOKE-CONTROL", "ask-free-control"),
                ("SMOKE-ASK", "depth2-ask"),
                ("SMOKE-FALLBACK", "model-fallback"),
            },
        )

    def test_task_contract_replaces_unresolved_template_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task.md"
            path.write_text(
                "# TEST\n\n## Identity\n\n"
                "- Task ID: TEST\n"
                "- Branch: task/TEST-smoke\n"
                f"- Worktree: {Path(temporary)}\n"
                "- Base branch: main\n"
                "- Base revision: deadbeef\n\n"
                "## Purpose\n\nTBD\n",
                encoding="utf-8",
            )
            runtime.write_contract(
                path,
                purpose="Concrete runtime diagnostic.",
                scope=["One bounded action."],
                acceptance=["Evidence is recorded."],
                test_plan=["Run the bounded control."],
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("TBD", text)
            self.assertIn("## Stop conditions", text)
            self.assertIn("- Status: initialized", text)
            self.assertIn("- [ ] Evidence is recorded.", text)

    def test_debug_launchers_use_official_diagnostic_paths_without_auto_approval(self) -> None:
        interactive = (ROOT / "tests" / "runtime" / "run_opencode_debug.sh").read_text(
            encoding="utf-8"
        )
        direct = (ROOT / "tests" / "runtime" / "run_direct_leaf.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--print-logs --log-level DEBUG", interactive)
        self.assertIn("snapshot-sessions", interactive)
        self.assertNotIn(" --auto", interactive)
        self.assertIn("--print-logs --log-level DEBUG run", direct)
        self.assertIn("--agent general", direct)
        self.assertIn("--format json", direct)
        self.assertNotIn(" --auto", direct)

    def test_report_template_requires_evidence_based_classification(self) -> None:
        metadata = {
            "templatesCommit": "abc",
            "opencodeVersion": "1.18.4",
            "template": "agent-python",
            "smokeRepo": "/tmp/smoke",
            "createdAt": "2026-08-09T00:00:00+00:00",
        }
        report = runtime.report_template("issue-41", metadata)
        self.assertIn("Ask-free nested control", report)
        self.assertIn("Direct leaf control", report)
        self.assertIn("Depth-2 Ask probe", report)
        self.assertIn("PASS / FAIL / INCOMPLETE", report)
        self.assertIn("Do not infer PASS", report)


if __name__ == "__main__":
    unittest.main()
