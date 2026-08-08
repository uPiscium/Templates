from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "init_context.py"
spec = importlib.util.spec_from_file_location("init_context", MODULE_PATH)
assert spec and spec.loader
init = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = init
spec.loader.exec_module(init)


class InitContractTest(unittest.TestCase):
    def test_default_branch_rejects_task_state(self) -> None:
        with self.assertRaisesRegex(init.InitError, "Task State must not exist"):
            init.validate_identity(Path("."), "main", "main", {"taskId": "TASK-1"})

    def test_task_branch_identity_mismatch_is_rejected(self) -> None:
        state = {
            "taskId": "TASK-1",
            "branch": "task/TASK-1-example",
            "worktree": str(Path(".").resolve()),
        }
        with self.assertRaisesRegex(init.InitError, "branch/Task identity mismatch"):
            init.validate_identity(Path("."), "task/TASK-2-example", "main", state)

    def test_version_mismatch_blocks_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / ".automation"
            automation.mkdir()
            (automation / "VERSION").write_text("999\n", encoding="utf-8")
            (automation / "ADAPTER").write_text("base\n", encoding="utf-8")
            with self.assertRaisesRegex(init.InitError, "unsupported Agent Core version"):
                init.context(root)

    def test_init_runtime_contains_no_repository_mutation_commands(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "git worktree add",
            "git checkout",
            "git switch",
            "git commit",
            "git push",
            "write_text(",
            "mkdir(",
            "unlink(",
            "rmtree(",
        ):
            self.assertNotIn(forbidden, text)

    def test_init_command_overrides_builtin_as_read_only(self) -> None:
        command = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "commands"
            / "init.md"
        ).read_text(encoding="utf-8")
        self.assertIn("overrides OpenCode's built-in `/init`", command)
        self.assertIn("Do not generate, rewrite, or repair `AGENTS.md`", command)

    def test_task_workflows_reuse_initialize_skill(self) -> None:
        commands = ROOT / "components" / "agent-core" / ".opencode" / "commands"
        for name in ("task-run.md", "task-batch.md"):
            text = (commands / name).read_text(encoding="utf-8")
            self.assertIn("initialize", text, name)
            self.assertIn("Stop if initialization fails", text, name)

    def test_adapter_fragment_is_part_of_init_contract(self) -> None:
        core = (
            ROOT / "components" / "agent-core" / ".automation" / "INIT.md"
        ).read_text(encoding="utf-8")
        fragment = (
            ROOT
            / "components"
            / "adapters"
            / "base"
            / ".automation"
            / "INIT.fragment.md"
        ).read_text(encoding="utf-8")
        self.assertIn(".automation/INIT.fragment.md", core)
        self.assertIn("just project::doctor", fragment)

    def test_generated_init_files_match_sources(self) -> None:
        pairs = [
            ("components/agent-core/.automation/INIT.md", "templates/agent-base/.automation/INIT.md"),
            ("components/agent-core/.automation/VERSION", "templates/agent-base/.automation/VERSION"),
            ("components/adapters/base/.automation/ADAPTER", "templates/agent-base/.automation/ADAPTER"),
            ("components/adapters/base/.automation/INIT.fragment.md", "templates/agent-base/.automation/INIT.fragment.md"),
            ("components/agent-core/.automation/bin/init_context.py", "templates/agent-base/.automation/bin/init_context.py"),
            ("components/agent-core/.automation/just/agent.just", "templates/agent-base/.automation/just/agent.just"),
            ("components/agent-core/.opencode/skills/initialize/SKILL.md", "templates/agent-base/.opencode/skills/initialize/SKILL.md"),
            ("components/agent-core/.opencode/commands/init.md", "templates/agent-base/.opencode/commands/init.md"),
            ("components/agent-core/.opencode/commands/task-run.md", "templates/agent-base/.opencode/commands/task-run.md"),
            ("components/agent-core/.opencode/commands/task-batch.md", "templates/agent-base/.opencode/commands/task-batch.md"),
            ("components/agent-core/.opencode/agents/build.md", "templates/agent-base/.opencode/agents/build.md"),
            ("components/agent-core/.opencode/agents/task-orchestrator.md", "templates/agent-base/.opencode/agents/task-orchestrator.md"),
            ("components/agent-core/AGENTS.md", "templates/agent-base/AGENTS.md"),
        ]
        for source, generated in pairs:
            self.assertEqual(
                (ROOT / source).read_bytes(),
                (ROOT / generated).read_bytes(),
                source,
            )


if __name__ == "__main__":
    unittest.main()
