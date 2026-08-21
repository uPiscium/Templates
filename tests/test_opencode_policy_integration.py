from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
)


class OpenCodePolicyIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        cls.lock = json.loads((ROOT / "flake.lock").read_text(encoding="utf-8"))

    def test_root_flake_declares_policy_input_and_nixpkgs_follow(self) -> None:
        self.assertIn('opencodePolicy', self.flake)
        self.assertIn('url = "github:upiscium/OpenCodePolicy";', self.flake)
        self.assertIn('inputs.nixpkgs.follows = "nixpkgs";', self.flake)
        self.assertRegex(self.flake, r"outputs\s*=\s*\{[^}]*\bopencodePolicy\b")

    def test_lock_pins_opencode_policy_repository_and_revision(self) -> None:
        node = self.lock["nodes"]["opencodePolicy"]
        self.assertEqual("upiscium", node["locked"]["owner"])
        self.assertEqual("OpenCodePolicy", node["locked"]["repo"])
        self.assertEqual(
            "103b47b67a7e650835ac277ed192dbc92d1639c4",
            node["locked"]["rev"],
        )
        self.assertRegex(node["locked"]["rev"], r"^[0-9a-f]{40}$")
        self.assertEqual(["nixpkgs"], node["inputs"]["nixpkgs"])
        self.assertEqual("opencodePolicy", self.lock["nodes"]["root"]["inputs"]["opencodePolicy"])

    def test_linux_only_policy_check_uses_explicit_agent_core_profile_and_self(self) -> None:
        self.assertIn("flake-utils.lib.eachDefaultSystem", self.flake)
        self.assertIn('if system == "x86_64-darwin" then { }', self.flake)
        self.assertIn("optionalAttrs isLinux", self.flake)
        self.assertIn("checks.opencode-policy", self.flake)
        self.assertIn("opencodePolicy.packages.${system}.opencode-policy", self.flake)
        self.assertRegex(
            self.flake,
            re.compile(
                r"opencode-policy audit-consumer\s+\\\s+"
                r"--profile agent-core\s+\\\s+"
                r"--consumer \$\{self\}\s+\\\s+"
                r"--strict",
                re.MULTILINE,
            ),
        )

    def test_generated_template_flakes_do_not_receive_policy_input(self) -> None:
        for template in TEMPLATES:
            flake = ROOT / "templates" / template / "flake.nix"
            self.assertNotIn("opencodePolicy", flake.read_text(encoding="utf-8"), template)

    def test_agent_core_version_and_upstream_are_expected(self) -> None:
        core = ROOT / "components" / "agent-core" / ".automation"
        self.assertEqual("3\n", (core / "VERSION").read_text(encoding="utf-8"))
        self.assertEqual(
            'repository = "github:upiscium/Templates"\nref = "main"\n'
            'component = "components/agent-core"\n',
            (core / "UPSTREAM").read_text(encoding="utf-8"),
        )

    def test_ci_builds_locked_policy_check_without_cloning_policy(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(encoding="utf-8")
        self.assertIn("cachix/install-nix-action@v31", workflow)
        self.assertIn(
            "nix flake check --all-systems --no-build --no-update-lock-file",
            workflow,
        )
        self.assertIn(
            "nix build .#checks.x86_64-linux.opencode-policy --no-link --no-update-lock-file",
            workflow,
        )
        self.assertNotRegex(workflow, r"git\s+clone.*OpenCodePolicy")


if __name__ == "__main__":
    unittest.main()
