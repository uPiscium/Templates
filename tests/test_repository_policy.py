from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core"
SCRIPT = CORE / ".automation" / "bin" / "repository_policy.py"
SPEC = importlib.util.spec_from_file_location("repository_policy", SCRIPT)
assert SPEC and SPEC.loader
policy_runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy_runtime
SPEC.loader.exec_module(policy_runtime)

TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
)


class RepositoryPolicyContractTest(unittest.TestCase):
    @staticmethod
    def _effective_rules(ruleset_id: int, *, required_count: int | None = 0):
        base = {
            "ruleset_id": ruleset_id,
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["merge", "squash", "rebase"],
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        }
        if required_count is not None:
            base["parameters"]["required_approving_review_count"] = required_count
        return [
            base,
            {"type": "deletion", "ruleset_id": ruleset_id},
            {"type": "non_fast_forward", "ruleset_id": ruleset_id},
        ]

    def _managed_ruleset_detail(self, ruleset_id: int, policy: dict[str, Any]):
        detail = deepcopy(policy["ruleset"])
        detail["id"] = ruleset_id
        return detail

    def test_policy_requires_main_and_pull_request_without_bypass(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        self.assertEqual(policy["default_branch"], "main")
        ruleset = policy["ruleset"]
        self.assertEqual(ruleset["name"], "Agent repository policy")
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(
            ruleset["conditions"],
            {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        )

        rules = {rule["type"]: rule for rule in ruleset["rules"]}
        self.assertEqual(set(rules), {"pull_request", "deletion", "non_fast_forward"})
        pull_request = rules["pull_request"]["parameters"]
        self.assertEqual(pull_request["required_approving_review_count"], 0)
        self.assertEqual(
            set(pull_request["allowed_merge_methods"]),
            {"merge", "squash", "rebase"},
        )
        self.assertFalse(pull_request["require_code_owner_review"])
        self.assertFalse(pull_request["require_last_push_approval"])

    def test_generated_agent_core_policy_files_match_source(self) -> None:
        paths = (
            ".automation/repository-policy.json",
            ".automation/bin/repository_policy.py",
            ".automation/just/repository.just",
            ".automation/REPOSITORY_POLICY.md",
            ".automation/ownership.toml",
            "Justfile",
            "opencode.json",
        )
        for template in TEMPLATES:
            generated = ROOT / "templates" / template
            for relative in paths:
                self.assertEqual(
                    (CORE / relative).read_bytes(),
                    (generated / relative).read_bytes(),
                    f"generated drift: {template}/{relative}",
                )

    def test_router_and_opencode_permission_boundary(self) -> None:
        justfile = (CORE / "Justfile").read_text(encoding="utf-8")
        self.assertIn("mod repository '.automation/just/repository.just'", justfile)

        module = (CORE / ".automation" / "just" / "repository.just").read_text(
            encoding="utf-8"
        )
        self.assertIn("policy-check:", module)
        self.assertIn("policy-apply:", module)

        config = json.loads((CORE / "opencode.json").read_text(encoding="utf-8"))
        bash = config["permission"]["bash"]
        self.assertEqual(bash["just repository::policy-check"], "allow")
        self.assertEqual(bash["just repository::policy-apply"], "ask")

    def test_policy_apply_is_forbidden_from_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".task-state"
            state.mkdir()
            (state / "task.md").write_text("# Task\n", encoding="utf-8")
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "forbidden from a Task worktree",
            ):
                policy_runtime.command_apply(root)

    def test_policy_apply_refuses_to_invent_missing_main_branch(self) -> None:
        policy = {
            "version": 1,
            "default_branch": "main",
            "ruleset": {},
        }
        before = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "master",
                "expected": "main",
                "expectedBranchExists": False,
                "match": False,
            },
            "ruleset": {"id": None, "match": False},
            "drift": ["default branch drift"],
        }
        with patch.object(policy_runtime, "task_worktree", return_value=False), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(policy_runtime, "inspect", return_value=before), patch.object(
            policy_runtime, "gh_api"
        ) as gh_api:
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "branch 'main' does not exist",
            ):
                policy_runtime.command_apply(Path("/tmp/repository"))
            gh_api.assert_not_called()

    def test_policy_apply_refuses_empty_repository_before_mutation(self) -> None:
        policy = {
            "version": 1,
            "default_branch": "main",
            "ruleset": {},
        }
        before = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "main",
                "expected": "main",
                "expectedBranchExists": False,
                "match": True,
            },
            "ruleset": {"id": None, "match": False},
            "drift": ["required branch 'main' does not exist"],
        }
        with patch.object(
            policy_runtime, "task_worktree", return_value=False
        ), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(
            policy_runtime, "inspect", return_value=before
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "branch 'main' does not exist",
            ):
                policy_runtime.command_apply(Path("/tmp/repository"))
            gh_api.assert_not_called()

    def test_policy_apply_sets_main_before_managing_ruleset(self) -> None:
        policy = {
            "version": 1,
            "default_branch": "main",
            "ruleset": {"name": "Agent repository policy"},
        }
        before = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "master",
                "expected": "main",
                "expectedBranchExists": True,
                "match": False,
            },
            "ruleset": {"id": None, "match": False},
            "drift": ["drift"],
        }
        after = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "main",
                "expected": "main",
                "expectedBranchExists": True,
                "match": True,
            },
            "ruleset": {"id": 1, "match": True},
            "drift": [],
        }
        calls: list[tuple[str, str, object]] = []

        def fake_api(root, method, endpoint, *, body=None, allow_not_found=False):
            calls.append((method, endpoint, body))
            return {}

        with patch.object(policy_runtime, "task_worktree", return_value=False), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(policy_runtime, "inspect", side_effect=[before, after]), patch.object(
            policy_runtime, "gh_api", side_effect=fake_api
        ):
            self.assertEqual(policy_runtime.command_apply(Path("/tmp/repository")), 0)

        self.assertEqual(calls[0], ("PATCH", "repos/example/repository", {"default_branch": "main"}))
        self.assertEqual(calls[1][0:2], ("POST", "repos/example/repository/rulesets"))

    def test_repository_policy_is_current_repository_only(self) -> None:
        parser = policy_runtime.parser()
        check = parser.parse_args(["check"])
        apply = parser.parse_args(["apply"])
        self.assertEqual(check.command, "check")
        self.assertEqual(apply.command, "apply")
        self.assertNotIn("repository", vars(check))
        self.assertNotIn("repository", vars(apply))

    def test_ownership_and_docs_cover_policy_surfaces(self) -> None:
        ownership = (CORE / ".automation" / "ownership.toml").read_text(encoding="utf-8")
        for path in (
            ".automation/REPOSITORY_POLICY.md",
            ".automation/repository-policy.json",
            ".automation/bin/repository_policy.py",
            ".automation/just/repository.just",
            "opencode.json",
        ):
            self.assertIn(f'"{path}" = "replace"', ownership)

        docs = (CORE / ".automation" / "REPOSITORY_POLICY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("default branch is `main`", docs)
        self.assertIn("pull request is mandatory", docs)
        self.assertIn("zero approving reviews", docs)
        self.assertIn("No bypass actor", docs)
        self.assertIn("does not create or rename branches", docs)

    def test_inspect_success_when_configured_and_effective_rules_match(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 777
        branch_rules = self._effective_rules(ruleset_id)

        with patch.object(policy_runtime, "current_repository", return_value=repository), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            gh_api.side_effect = [
                {"default_branch": "main", "visibility": "private"},
                [
                    {
                        "name": "Agent repository policy",
                        "source_type": "Repository",
                        "id": ruleset_id,
                    }
                ],
                self._managed_ruleset_detail(ruleset_id, policy),
                branch_rules,
            ]
            result = policy_runtime.inspect(Path("/tmp/repository"), policy)

        self.assertTrue(result["ruleset"]["match"])
        self.assertTrue(result["effectiveRules"]["match"])
        self.assertEqual(result["drift"], [])
        self.assertEqual(result["visibility"], "private")
        self.assertEqual(result["effectiveRules"]["actual"][0]["type"], "deletion")

    def test_inspect_configured_match_without_effective_rules(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 4

        with patch.object(policy_runtime, "current_repository", return_value=repository), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            gh_api.side_effect = [
                {"default_branch": "main", "visibility": "public"},
                [
                    {
                        "name": "Agent repository policy",
                        "source_type": "Repository",
                        "id": ruleset_id,
                    }
                ],
                self._managed_ruleset_detail(ruleset_id, policy),
                [],
            ]
            result = policy_runtime.inspect(Path("/tmp/repository"), policy)

        self.assertTrue(result["ruleset"]["match"])
        self.assertFalse(result["effectiveRules"]["match"])
        self.assertIn(
            "configured ruleset matches policy, but it is not effective on main",
            result["drift"][0],
        )

    def test_inspect_reports_wrong_effective_ruleset_source(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 10
        wrong_ruleset_id = 11
        branch_rules = self._effective_rules(wrong_ruleset_id)

        with patch.object(policy_runtime, "current_repository", return_value=repository), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            gh_api.side_effect = [
                {"default_branch": "main", "visibility": "public"},
                [
                    {
                        "name": "Agent repository policy",
                        "source_type": "Repository",
                        "id": ruleset_id,
                    }
                ],
                self._managed_ruleset_detail(ruleset_id, policy),
                branch_rules,
            ]
            result = policy_runtime.inspect(Path("/tmp/repository"), policy)

        self.assertFalse(result["effectiveRules"]["match"])
        self.assertIn(
            "found source ruleset IDs=[11]",
            " ".join(result["drift"]),
        )

    def test_inspect_effective_rules_require_pull_request_deletion_and_non_fast_forward(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 10
        branch_rules = [
            self._effective_rules(ruleset_id)[0],
        ]

        with patch.object(policy_runtime, "current_repository", return_value=repository), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            gh_api.side_effect = [
                {"default_branch": "main", "visibility": "public"},
                [
                    {
                        "name": "Agent repository policy",
                        "source_type": "Repository",
                        "id": ruleset_id,
                    }
                ],
                self._managed_ruleset_detail(ruleset_id, policy),
                branch_rules,
            ]
            result = policy_runtime.inspect(Path("/tmp/repository"), policy)

        self.assertFalse(result["effectiveRules"]["match"])
        drift = result["drift"][0]
        self.assertIn("not effective on main", drift)
        self.assertIn("deletion", drift)

    def test_inspect_effective_pull_request_approving_review_count_match_and_absent(
        self,
    ) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 22

        for required_count in (1, None):
            branch_rules = self._effective_rules(
                ruleset_id, required_count=required_count
            )

            with self.subTest(required_count=required_count), patch.object(
                policy_runtime, "current_repository", return_value=repository
            ), patch.object(policy_runtime, "branch_exists", return_value=True), patch.object(
                policy_runtime, "gh_api"
            ) as gh_api:
                gh_api.side_effect = [
                    {"default_branch": "main", "visibility": "public"},
                    [
                        {
                            "name": "Agent repository policy",
                            "source_type": "Repository",
                            "id": ruleset_id,
                        }
                    ],
                    self._managed_ruleset_detail(ruleset_id, policy),
                    branch_rules,
                ]
                result = policy_runtime.inspect(Path("/tmp/repository"), policy)

            if required_count == 1:
                self.assertFalse(result["effectiveRules"]["match"])
                self.assertIn("required_approving_review_count=1", " ".join(result["effectiveRules"]["drift"]))
            else:
                self.assertTrue(result["effectiveRules"]["match"])
                self.assertNotIn(
                    "required_approving_review_count",
                    " ".join(result["effectiveRules"]["drift"]),
                )

    def test_inspect_does_not_infer_unreturned_pull_request_parameters(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 23
        branch_rules = self._effective_rules(ruleset_id)
        branch_rules[0].pop("parameters")

        with patch.object(
            policy_runtime, "current_repository", return_value=repository
        ), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ), patch.object(policy_runtime, "gh_api") as gh_api:
            gh_api.side_effect = [
                {"default_branch": "main", "visibility": "public"},
                [
                    {
                        "name": "Agent repository policy",
                        "source_type": "Repository",
                        "id": ruleset_id,
                    }
                ],
                self._managed_ruleset_detail(ruleset_id, policy),
                branch_rules,
            ]
            result = policy_runtime.inspect(Path("/tmp/repository"), policy)

        self.assertTrue(result["effectiveRules"]["match"])

    def test_inspect_api_error_propagates_from_effective_rules_endpoint(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        repository = "owner/example"
        ruleset_id = 3

        with patch.object(policy_runtime, "current_repository", return_value=repository), patch.object(
            policy_runtime, "branch_exists", return_value=True
        ):

            def side_effect(root, method, endpoint, *, body=None, allow_not_found=False):
                if endpoint.endswith("/rules/branches/main"):
                    raise policy_runtime.RepositoryPolicyError("rate limited")
                if endpoint == "repos/owner/example":
                    return {"default_branch": "main", "visibility": "public"}
                if endpoint == "repos/owner/example/rulesets?includes_parents=false&per_page=100":
                    return [
                        {
                            "name": "Agent repository policy",
                            "source_type": "Repository",
                            "id": ruleset_id,
                        }
                    ]
                if endpoint == "repos/owner/example/rulesets/3":
                    return self._managed_ruleset_detail(ruleset_id, policy)
                raise AssertionError(f"unexpected endpoint {endpoint}")

            with patch.object(policy_runtime, "gh_api", side_effect=side_effect):
                with self.assertRaisesRegex(
                    policy_runtime.RepositoryPolicyError, "rate limited"
                ):
                    policy_runtime.inspect(Path("/tmp/repository"), policy)

    def test_command_apply_rejects_incomplete_post_verification(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        before = {
            "repository": "owner/example",
            "defaultBranch": {
                "actual": "main",
                "expected": "main",
                "expectedBranchExists": True,
                "match": True,
            },
            "ruleset": {"id": 1, "match": True},
            "drift": [],
        }
        after = {
            "drift": ["effective rules did not fully apply"],
        }

        with patch.object(policy_runtime, "task_worktree", return_value=False), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(policy_runtime, "inspect", side_effect=[before, after]):
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "verification still reports drift",
            ):
                policy_runtime.command_apply(Path("/tmp/repository"))


if __name__ == "__main__":
    unittest.main()
