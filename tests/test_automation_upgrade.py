from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
SPEC = importlib.util.spec_from_file_location("automation_upgrade", SCRIPT)
assert SPEC and SPEC.loader
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class AutomationUpgradeContractTest(unittest.TestCase):
    def test_upstream_and_generated_parity(self) -> None:
        upstream = ROOT / "components" / "agent-core" / ".automation" / "UPSTREAM"
        ownership = ROOT / "components" / "agent-core" / ".automation" / "ownership.toml"
        self.assertIn('repository = "github:upiscium/Templates"', upstream.read_text())
        self.assertIn('"AGENTS.md" = "replace"', ownership.read_text())
        for template in ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertEqual(
                upstream.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "UPSTREAM").read_bytes(),
            )
            self.assertEqual(
                ownership.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "ownership.toml").read_bytes(),
            )
            self.assertEqual(
                SCRIPT.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "bin" / "automation_upgrade.py").read_bytes(),
            )

    def test_root_router_and_permissions(self) -> None:
        justfile = (ROOT / "components" / "agent-core" / "Justfile").read_text()
        self.assertIn("mod automation '.automation/just/automation.just'", justfile)
        cfg = json.loads((ROOT / "components" / "agent-core" / "opencode.json").read_text())
        bash = cfg["permission"]["bash"]
        self.assertEqual(bash["just automation::version"], "allow")
        self.assertEqual(bash["just automation::check-update *"], "allow")
        self.assertEqual(bash["just automation::upgrade *"], "ask")

    def test_upgrade_preserves_adapter_and_repository_owned_paths(self) -> None:
        script = SCRIPT.read_text()
        readme = (ROOT / "README.md").read_text()
        for protected in (
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
        ):
            self.assertIn(protected, script)
        self.assertIn("just/project/**", readme)
        self.assertIn("repository CI", readme)
        self.assertIn("AUTOMATION_MAINTENANCE", script)
        self.assertIn("upgrade refused on default branch", script)
        self.assertIn("commitCreated", script)
        self.assertIn("pushPerformed", script)
        self.assertIn("mergePerformed", script)

    def test_existing_repository_plan_merges_managed_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            automation = repo / ".automation"
            automation.mkdir()
            (automation / "VERSION").write_text("1\n")
            (automation / "ADAPTER").write_text("base\n")
            (automation / "UPSTREAM").write_text(
                'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n'
            )
            # Deliberately retain generated ownership metadata: adoption markers
            # must take precedence over the default replace mode.
            (automation / "ownership.toml").write_text(
                'version = 1\n\n[paths]\n"AGENTS.md" = "replace"\n"Justfile" = "replace"\n'
            )
            core_rules = (ROOT / "components" / "agent-core" / "AGENTS.md").read_text()
            (repo / "AGENTS.md").write_text(
                "# Existing Repository Rules\n\n"
                "<!-- BEGIN AGENT CORE RULES -->\nold core rules\n<!-- END AGENT CORE RULES -->\n"
            )
            (repo / "Justfile").write_text(
                "default:\n    @echo existing\n\n"
                "# Agent Core module router\n"
                "mod agent '.automation/just/agent.just'\n"
                "mod integrate '.automation/just/integrate.just'\n"
                "mod project 'just/project/mod.just'\n"
                "mod? local 'just/local.just'\n"
            )

            plan = upgrade.build_plan(repo, ROOT)
            actions = {item["path"]: item for item in plan["actions"]}
            self.assertEqual(actions["AGENTS.md"]["action"], "merge")
            self.assertEqual(actions["Justfile"]["action"], "merge")
            self.assertTrue(plan["canApply"], plan["blockers"])

            merged_agents, _ = upgrade.replace_agent_rules(
                (repo / "AGENTS.md").read_text(), core_rules
            )
            assert merged_agents is not None
            self.assertIn("# Existing Repository Rules", merged_agents)
            self.assertIn(core_rules.rstrip(), merged_agents)

            merged_just, _ = upgrade.merge_just_router(
                (repo / "Justfile").read_text(),
                (ROOT / "components" / "agent-core" / "Justfile").read_text(),
            )
            assert merged_just is not None
            self.assertIn("@echo existing", merged_just)
            self.assertIn("mod automation '.automation/just/automation.just'", merged_just)

    def test_malformed_rules_and_conflicting_router_block_upgrade(self) -> None:
        bad_agents, reason = upgrade.replace_agent_rules(
            "<!-- BEGIN AGENT CORE RULES -->\nmissing end\n", "new rules\n"
        )
        self.assertIsNone(bad_agents)
        self.assertIn("malformed", reason)

        bad_just, reason = upgrade.merge_just_router(
            "mod agent 'custom/agent.just'\n",
            (ROOT / "components" / "agent-core" / "Justfile").read_text(),
        )
        self.assertIsNone(bad_just)
        self.assertIn("repository-owned path", reason)

    def test_readme_covers_operational_entrypoints(self) -> None:
        readme = (ROOT / "README.md").read_text()
        for phrase in (
            "just automation::version",
            "just automation::check-update",
            "just automation::upgrade",
            "just template::adopt-plan",
            "base",
            "Task Orchestrator",
            "Ask",
            "just project::check",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
