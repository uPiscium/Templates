from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "update-opencode-policy.yml"


class OpenCodePolicyUpdateWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.trigger = cls.workflow.split("permissions:", 1)[0]
        cls.validate = cls.workflow.split("  validate:", 1)[1].split(
            "  publish:", 1
        )[0]
        cls.publish = cls.workflow.split("  publish:", 1)[1]

    def test_trigger_is_manual_only(self) -> None:
        self.assertIn("workflow_dispatch:", self.trigger)
        self.assertNotRegex(self.trigger, r"(?m)^\s+schedule:")
        self.assertNotRegex(self.trigger, r"(?m)^\s+pull_request:")
        self.assertNotRegex(self.trigger, r"(?m)^\s+push:")

    def test_dispatch_is_restricted_to_main(self) -> None:
        self.assertIn('DISPATCH_REF: ${{ github.ref }}', self.validate)
        self.assertIn('"refs/heads/main"', self.validate)
        self.assertRegex(self.validate, r"DISPATCH_REF.*refs/heads/main[\s\S]*exit 1")

    def test_job_permissions_are_isolated(self) -> None:
        self.assertRegex(
            self.validate,
            r"permissions:\s*\n\s+contents: read",
        )
        self.assertRegex(
            self.publish,
            r"permissions:\s*\n\s+contents: write\s*\n\s+pull-requests: write",
        )
        self.assertIn("persist-credentials: false", self.validate)

    def test_candidate_validation_uses_agent_core_profile(self) -> None:
        self.assertIn("nix flake update opencodePolicy", self.validate)
        self.assertRegex(self.validate, r"--profile\s+agent-core")
        policy_environment = (
            "nix develop --no-update-lock-file "
            ".#checks.x86_64-linux.opencode-policy --command"
        )
        self.assertEqual(2, self.validate.count(policy_environment))
        self.assertIn(f"{policy_environment} opencode-policy validate", self.validate)
        self.assertIn("candidate_sha256", self.workflow)
        self.assertIn("validated candidate checksum mismatch", self.publish)
        self.assertIn("check_opencode_policy_lock_update.py", self.publish)

    def test_publication_is_draft_and_lock_only(self) -> None:
        self.assertIn("--draft", self.publish)
        self.assertIn("git add -- flake.lock", self.publish)
        self.assertNotRegex(self.publish, r"git add (?:\.|-A|--all)\b")
        self.assertNotRegex(
            self.publish,
            r"(?:gh\s+pr\s+merge|git\s+merge|--auto(?:-merge)?)\b",
        )
        self.assertNotRegex(self.publish, r"git push[^\n]*(?:--force|\s-f(?:\s|$))")

    def test_updater_does_not_duplicate_generated_runtime_matrix(self) -> None:
        for template in ("agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertNotIn(template, self.workflow)


if __name__ == "__main__":
    unittest.main()
