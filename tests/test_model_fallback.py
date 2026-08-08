from __future__ import annotations

import importlib.util
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "model_fallback.py"
spec = importlib.util.spec_from_file_location("model_fallback", MODULE_PATH)
assert spec and spec.loader
fallback = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fallback
spec.loader.exec_module(fallback)


class ModelFallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy_path = ROOT / "components" / "agent-core" / ".automation" / "model-fallback.toml"
        cls.cfg = tomllib.loads(cls.policy_path.read_text(encoding="utf-8"))

    def test_usage_limit_error_is_fallback_eligible(self) -> None:
        result = fallback.classify("rate_limit_exceeded: usage limit reached", 429, self.cfg)
        self.assertTrue(result["fallback"])

    def test_auth_and_permission_errors_do_not_fallback(self) -> None:
        for text in ("authentication failed", "permission denied", "context window exceeded", "tool error"):
            result = fallback.classify(text, None, self.cfg)
            self.assertFalse(result["fallback"], text)

    def test_task_orchestrator_has_explicit_next_agent(self) -> None:
        result = fallback.next_fallback("task-orchestrator", "task-orchestrator", self.cfg)
        self.assertTrue(result["available"])
        self.assertEqual(result["agent"], "task-orchestrator-fallback")
        self.assertEqual(result["model"], "openai/gpt-5.6-sol")

    def test_chain_exhaustion_stops(self) -> None:
        result = fallback.next_fallback("general", "general-fallback", self.cfg)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "fallback chain exhausted")

    def test_main_fallback_is_not_automatic(self) -> None:
        result = fallback.next_fallback("build", "build", self.cfg)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "automatic fallback disabled")

    def test_fallback_evidence_is_recorded_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / ".task-state"
            state_dir.mkdir()
            state = state_dir / "task.md"
            state.write_text("# TASK\n\n## Evidence\n", encoding="utf-8")
            fallback.append_evidence(
                root,
                "general",
                "openai/gpt-5.3-codex-spark",
                "openai/gpt-5.6-sol",
                "usage limit",
                "succeeded",
            )
            text = state.read_text(encoding="utf-8")
            self.assertIn("### Model fallback", text)
            self.assertIn("usage limit", text)
            self.assertNotIn("api_key", text.lower())

    def test_generated_files_match_sources(self) -> None:
        pairs = [
            (
                ROOT / "components" / "agent-core" / ".automation" / "model-fallback.toml",
                ROOT / "templates" / "agent-base" / ".automation" / "model-fallback.toml",
            ),
            (
                ROOT / "components" / "agent-core" / ".automation" / "bin" / "model_fallback.py",
                ROOT / "templates" / "agent-base" / ".automation" / "bin" / "model_fallback.py",
            ),
            (
                ROOT / "components" / "agent-core" / ".automation" / "just" / "agent.just",
                ROOT / "templates" / "agent-base" / ".automation" / "just" / "agent.just",
            ),
            (
                ROOT / "components" / "agent-core" / ".opencode" / "agents" / "task-orchestrator.md",
                ROOT / "templates" / "agent-base" / ".opencode" / "agents" / "task-orchestrator.md",
            ),
        ]
        for source, generated in pairs:
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())


if __name__ == "__main__":
    unittest.main()
