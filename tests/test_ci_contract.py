from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CiContractTest(unittest.TestCase):
    def test_ci_covers_all_generated_language_adapters(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(encoding="utf-8")
        for template in ("agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertIn(f"- {template}", workflow)
        self.assertIn("python3 tools/render_templates.py check", workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", workflow)
        self.assertIn("just agent::doctor", workflow)
        self.assertIn("just agent::context", workflow)
        self.assertIn("just project::doctor", workflow)
        self.assertIn("just project::check", workflow)

    def test_ci_has_read_only_repository_permission(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_depth2_ask_result_is_not_claimed_pass(self) -> None:
        document = (ROOT / "docs" / "opencode-depth2-ask-smoke.md").read_text(encoding="utf-8")
        self.assertIn("UNVERIFIED", document)
        self.assertIn("PASS / FAIL / INCOMPLETE", document)
        status_section = document.split("## Status", 1)[1].split("## Purpose", 1)[0]
        self.assertNotIn("PASS", status_section)


if __name__ == "__main__":
    unittest.main()
