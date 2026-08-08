from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AutomationUpgradeContractTest(unittest.TestCase):
    def test_upstream_and_generated_parity(self) -> None:
        upstream = ROOT / "components" / "agent-core" / ".automation" / "UPSTREAM"
        self.assertIn('repository = "github:upiscium/Templates"', upstream.read_text())
        for template in ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertEqual(
                upstream.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "UPSTREAM").read_bytes(),
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
        script = (ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py").read_text()
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
