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

    def test_spark_roles_share_luna_fallback_chain(self) -> None:
        for role in ("general", "explore", "verifier", "scout"):
            result = fallback.next_fallback(role, role, self.cfg)
            self.assertTrue(result["available"], role)
            self.assertEqual(result["agent"], f"{role}-fallback", role)
            self.assertEqual(result["model"], "openai/gpt-5.6-luna", role)

    def test_primary_and_fallback_models_in_policy(self) -> None:
        expected = {
            "general": ("openai/gpt-5.3-codex-spark", "openai/gpt-5.6-luna"),
            "explore": ("openai/gpt-5.3-codex-spark", "openai/gpt-5.6-luna"),
            "verifier": ("openai/gpt-5.3-codex-spark", "openai/gpt-5.6-luna"),
            "scout": ("openai/gpt-5.3-codex-spark", "openai/gpt-5.6-luna"),
            "architect": ("openai/gpt-5.6-sol", "openai/gpt-5.3-codex-spark"),
            "reviewer": ("openai/gpt-5.6-terra", "openai/gpt-5.3-codex-spark"),
            "task-orchestrator": ("openai/gpt-5.3-codex-spark", "openai/gpt-5.6-sol"),
        }
        for role, (primary_model, fallback_model) in expected.items():
            role_cfg = self.cfg["roles"][role]
            self.assertEqual(role_cfg["primary_model"], primary_model, role)
            self.assertEqual(role_cfg["fallback_models"], [fallback_model], role)
            self.assertEqual(role_cfg["fallback_agents"], [f"{role}-fallback"], role)

    def test_no_five_six_primary_falls_back_to_five_six(self) -> None:
        for role, role_cfg in self.cfg["roles"].items():
            if role_cfg["primary_model"].startswith("openai/gpt-5.6-"):
                self.assertFalse(
                    any(model.startswith("openai/gpt-5.6-") for model in role_cfg["fallback_models"]),
                    role,
                )

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
                "openai/gpt-5.6-luna",
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
            (
                ROOT / "components" / "agent-core" / ".opencode" / "skills" / "task-orchestration" / "SKILL.md",
                ROOT / "templates" / "agent-base" / ".opencode" / "skills" / "task-orchestration" / "SKILL.md",
            ),
        ]
        for source, generated in pairs:
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())

        source_agents = ROOT / "components" / "agent-core" / ".opencode" / "agents"
        generated_agents = ROOT / "templates" / "agent-base" / ".opencode" / "agents"
        fallback_agents = sorted(source_agents.glob("*-fallback.md"))
        self.assertGreaterEqual(len(fallback_agents), 10)
        for source in fallback_agents:
            generated = generated_agents / source.name
            self.assertTrue(generated.is_file(), generated.as_posix())
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())


if __name__ == "__main__":
    unittest.main()
