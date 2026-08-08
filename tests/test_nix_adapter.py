from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NixAdapterContractTest(unittest.TestCase):
    def test_manifest_and_generated_adapter(self) -> None:
        manifest = json.loads((ROOT / "templates" / "manifest.json").read_text())
        self.assertEqual(manifest["templates"]["agent-nix"]["adapter"], "nix")
        self.assertEqual((ROOT / "templates" / "agent-nix" / ".automation" / "ADAPTER").read_text().strip(), "nix")

    def test_stable_project_api(self) -> None:
        text = (ROOT / "components" / "adapters" / "nix" / "just" / "project" / "mod.just").read_text()
        for recipe in ("doctor:", "eval:", "format-check:", "lint:", "test:", "build:", "check:"):
            self.assertIn(recipe, text)
        self.assertIn("nix build --no-link", text)
        self.assertNotIn("nix store delete", text)
        self.assertNotIn("switch", text)

    def test_generated_source_parity(self) -> None:
        source = ROOT / "components" / "adapters" / "nix"
        generated = ROOT / "templates" / "agent-nix"
        for relative in (
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
            ".envrc",
            "README.md",
            "flake.nix",
            "just/project/mod.just",
            "just/project/nix.just",
            "just/project/repository.just",
        ):
            self.assertEqual((source / relative).read_bytes(), (generated / relative).read_bytes(), relative)

    def test_opencode_only_allows_guarded_eval(self) -> None:
        cfg = json.loads((ROOT / "components" / "agent-core" / "opencode.json").read_text())
        bash = cfg["permission"]["bash"]
        self.assertEqual(bash["just project::eval"], "allow")
        self.assertNotIn("nix *", {key for key, value in bash.items() if value == "allow"})


if __name__ == "__main__":
    unittest.main()
