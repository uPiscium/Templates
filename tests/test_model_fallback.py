from __future__ import annotations

import importlib.util
import json
import shutil
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

    def test_luna_roles_share_spark_fallback_chain(self) -> None:
        for role in ("general", "explore"):
            result = fallback.next_fallback(role, role, self.cfg)
            self.assertTrue(result["available"], role)
            self.assertEqual(result["agent"], f"{role}-fallback", role)
            self.assertEqual(result["model"], "openai/gpt-5.3-codex-spark", role)

    def test_primary_and_fallback_models_in_policy(self) -> None:
        expected = {
            "plan": ("openai/gpt-5.6-sol", "openai/gpt-5.3-codex-spark"),
            "general": ("openai/gpt-5.6-luna", "openai/gpt-5.3-codex-spark"),
            "explore": ("openai/gpt-5.6-luna", "openai/gpt-5.3-codex-spark"),
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

    def test_every_fallback_crosses_quota_families(self) -> None:
        for role, role_cfg in self.cfg["roles"].items():
            primary_family = fallback.model_family(role_cfg["primary_model"], self.cfg)
            self.assertIsNotNone(primary_family, role)
            for fallback_model in role_cfg["fallback_models"]:
                fallback_family = fallback.model_family(fallback_model, self.cfg)
                self.assertIsNotNone(fallback_family, role)
                self.assertNotEqual(primary_family, fallback_family, role)

    def test_chain_exhaustion_stops(self) -> None:
        result = fallback.next_fallback("general", "general-fallback", self.cfg)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "fallback chain exhausted")

    def test_recovery_routes_spark_family_according_to_policy(self) -> None:
        expected = {
            "task-orchestrator": ("fallback", "gpt-5.6", "task-orchestrator-fallback"),
            "general": ("primary", "gpt-5.6", "general"),
            "explore": ("primary", "gpt-5.6", "explore"),
            "verifier": ("fallback", "gpt-5.6", "verifier-fallback"),
            "scout": ("fallback", "gpt-5.6", "scout-fallback"),
        }
        for role, (status, family, agent) in expected.items():
            result = fallback.recovery_route(role, "spark", self.cfg)
            self.assertEqual(result["status"], status, role)
            self.assertEqual(result["family"], family, role)
            self.assertEqual(result["agent"], agent, role)

    def test_recovery_routes_gpt_family_according_to_policy(self) -> None:
        expected = {
            "general": ("fallback", "spark", "general-fallback"),
            "explore": ("fallback", "spark", "explore-fallback"),
            "verifier": ("primary", "spark", "verifier"),
            "scout": ("primary", "spark", "scout"),
            "architect": ("fallback", "spark", "architect-fallback"),
            "reviewer": ("fallback", "spark", "reviewer-fallback"),
            "investigator": ("fallback", "spark", "investigator-fallback"),
            "security-reviewer": ("fallback", "spark", "security-reviewer-fallback"),
        }
        for role, (status, family, agent) in expected.items():
            result = fallback.recovery_route(role, "gpt-5.6", self.cfg)
            self.assertEqual(result["status"], status, role)
            self.assertEqual(result["family"], family, role)
            self.assertEqual(result["agent"], agent, role)

    def test_recovery_unknown_family_fails_closed(self) -> None:
        result = fallback.recovery_route("general", "mystery", self.cfg)
        self.assertEqual(result["status"], "BLOCKED")

    def test_recovery_chain_exhaustion_is_machine_readable(self) -> None:
        cfg = {"families": {"spark": ["spark-model"]}, "roles": {
            "r": {"primary_agent": "r", "primary_model": "spark-model",
                   "fallback_agents": [], "fallback_models": []}}}
        result = fallback.recovery_route("r", "spark", cfg)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "fallback chain exhausted")

    def test_cross_role_agent_substitution_fails_closed(self) -> None:
        cfg = {"roles": {"general": {
            "primary_agent": "general", "primary_model": "spark-model",
            "fallback_agents": ["build"], "fallback_models": ["other-model"],
        }}}
        with self.assertRaisesRegex(fallback.FallbackError, "cross-role fallback agent"):
            fallback.role_chain("general", cfg)

    def test_policy_models_are_bound_to_agent_frontmatter(self) -> None:
        core = ROOT / "components" / "agent-core"
        for role in self.cfg["roles"]:
            fallback.validate_agent_binding(role, self.cfg, [core])
        with tempfile.TemporaryDirectory() as directory:
            task_root = Path(directory)
            agents = task_root / ".opencode" / "agents"
            agents.mkdir(parents=True)
            for name in ("general", "general-fallback"):
                shutil.copy2(core / ".opencode" / "agents" / f"{name}.md", agents / f"{name}.md")
            fallback_agent = agents / "general-fallback.md"
            fallback_agent.write_text(
                fallback_agent.read_text(encoding="utf-8").replace(
                    "model: openai/gpt-5.3-codex-spark", "model: openai/gpt-5.6-terra"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(fallback.FallbackError, "agent model mismatch"):
                fallback.validate_agent_binding("general", self.cfg, [task_root])

    def test_effective_permission_contract_drift_fails_closed(self) -> None:
        core = ROOT / "components" / "agent-core"
        with tempfile.TemporaryDirectory() as directory:
            task_root = Path(directory) / "task"
            main_root = Path(directory) / "main"
            for root in (task_root, main_root):
                agents = root / ".opencode" / "agents"
                agents.mkdir(parents=True)
                for name in ("general", "general-fallback"):
                    shutil.copy2(core / ".opencode" / "agents" / f"{name}.md", agents / f"{name}.md")
                shutil.copy2(core / "opencode.json", root / "opencode.json")
            for name in ("general", "general-fallback"):
                path = main_root / ".opencode" / "agents" / f"{name}.md"
                path.write_text(
                    path.read_text(encoding="utf-8").replace(
                        '    "just project::build": allow', '    "just project::build": deny'
                    ),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(fallback.FallbackError, "cross-worktree authority mismatch"):
                fallback.validate_agent_binding("general", self.cfg, [task_root, main_root])

            config = json.loads((main_root / "opencode.json").read_text(encoding="utf-8"))
            config["permission"]["bash"]["just project::build"] = "deny"
            (main_root / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(fallback.FallbackError, "project permission mismatch"):
                fallback.validate_project_permission_binding([task_root, main_root])

    def test_main_fallback_is_not_automatic(self) -> None:
        result = fallback.next_fallback("build", "build", self.cfg)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "automatic fallback disabled")

    def test_plan_fallback_is_manual_and_cross_family(self) -> None:
        role = self.cfg["roles"]["plan"]
        self.assertEqual(role["primary_agent"], "plan")
        self.assertEqual(role["primary_model"], "openai/gpt-5.6-sol")
        self.assertEqual(role["fallback_agents"], ["plan-fallback"])
        self.assertEqual(role["fallback_models"], ["openai/gpt-5.3-codex-spark"])
        self.assertFalse(role["automatic"])
        self.assertNotEqual(
            fallback.model_family(role["primary_model"], self.cfg),
            fallback.model_family(role["fallback_models"][0], self.cfg),
        )
        result = fallback.next_fallback("plan", "plan", self.cfg)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "automatic fallback disabled")

    def test_fallback_evidence_is_recorded_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / ".automation"
            automation.mkdir()
            (automation / "model-fallback.toml").write_bytes(self.policy_path.read_bytes())
            state_dir = root / ".task-state"
            state_dir.mkdir()
            state = state_dir / "task.md"
            state.write_text("# TASK\n\n## Evidence\n", encoding="utf-8")
            fallback.append_evidence(
                root,
                "general",
                "openai/gpt-5.6-luna",
                "openai/gpt-5.3-codex-spark",
                "usage limit",
                "succeeded",
            )
            text = state.read_text(encoding="utf-8")
            self.assertIn("### Model fallback", text)
            self.assertIn("usage limit", text)
            self.assertNotIn("api_key", text.lower())

    def test_fallback_evidence_rejects_unclassified_or_unconfigured_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / ".automation"
            automation.mkdir()
            (automation / "model-fallback.toml").write_bytes(self.policy_path.read_bytes())
            state_dir = root / ".task-state"
            state_dir.mkdir()
            (state_dir / "task.md").write_text("## Evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(fallback.FallbackError, "classified usage-limit"):
                fallback.append_evidence(root, "general", self.cfg["roles"]["general"]["primary_model"],
                                         self.cfg["roles"]["general"]["fallback_models"][0],
                                         "permission denied", "failed")
            with self.assertRaisesRegex(fallback.FallbackError, "configured role chain"):
                fallback.append_evidence(root, "general", "secret-model", "other-model", "usage limit", "failed")

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
        self.assertGreaterEqual(len(fallback_agents), 11)
        for source in fallback_agents:
            generated = generated_agents / source.name
            self.assertTrue(generated.is_file(), generated.as_posix())
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())


if __name__ == "__main__":
    unittest.main()
