#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

API_VERSION = "2026-03-10"
RULESET_NAME = "Agent repository policy"
PULL_REQUEST_PARAMETERS = {
    "allowed_merge_methods": ["merge", "squash", "rebase"],
    "dismiss_stale_reviews_on_push": False,
    "require_code_owner_review": False,
    "require_last_push_approval": False,
    "required_approving_review_count": 0,
    "required_review_thread_resolution": False,
}
EFFECTIVE_RULE_TYPES = ("deletion", "non_fast_forward", "pull_request")


class RepositoryPolicyError(RuntimeError):
    pass


def repository_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RepositoryPolicyError("not inside a Git repository")
    return Path(result.stdout.strip()).resolve()


def load_policy(root: Path) -> dict[str, Any]:
    path = root / ".automation" / "repository-policy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositoryPolicyError(f"missing repository policy: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RepositoryPolicyError(f"invalid repository policy JSON: {exc}") from exc

    if set(data) != {"version", "default_branch", "ruleset"}:
        raise RepositoryPolicyError(
            "repository policy must contain exactly version, default_branch, ruleset"
        )
    if data["version"] != 1:
        raise RepositoryPolicyError(
            f"unsupported repository policy version: {data['version']!r}"
        )
    if data["default_branch"] != "main":
        raise RepositoryPolicyError("repository policy default_branch must be 'main'")

    expected_ruleset = {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "pull_request",
                "parameters": PULL_REQUEST_PARAMETERS,
            },
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    }
    if data["ruleset"] != expected_ruleset:
        raise RepositoryPolicyError(
            "repository policy ruleset does not match the supported Agent Core contract"
        )
    return data


def _run_json(
    command: list[str],
    *,
    cwd: Path,
    stdin: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        input=None if stdin is None else json.dumps(stdin),
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if allow_not_found and ("HTTP 404" in detail or "Not Found" in detail):
            return None
        raise RepositoryPolicyError(
            f"{' '.join(command)} failed: {detail or f'exit {result.returncode}'}"
        )
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RepositoryPolicyError(
            f"{' '.join(command)} returned invalid JSON"
        ) from exc


def gh_api(
    root: Path,
    method: str,
    endpoint: str,
    *,
    body: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    command = [
        "gh",
        "api",
        "--method",
        method,
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        endpoint,
    ]
    if body is not None:
        command.extend(["--input", "-"])
    return _run_json(
        command,
        cwd=root,
        stdin=body,
        allow_not_found=allow_not_found,
    )


def current_repository(root: Path) -> str:
    value = _run_json(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=root,
    )
    repository = value.get("nameWithOwner") if isinstance(value, dict) else None
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise RepositoryPolicyError(
            "unable to resolve the current GitHub repository from this checkout"
        )
    return repository


def task_worktree(root: Path) -> bool:
    return (root / ".task-state" / "task.md").is_file()


def branch_exists(root: Path, repository: str, branch: str) -> bool:
    value = gh_api(
        root,
        "GET",
        f"repos/{repository}/branches/{branch}",
        allow_not_found=True,
    )
    return value is not None


def desired_ruleset(policy: dict[str, Any]) -> dict[str, Any]:
    return policy["ruleset"]


def normalize_ruleset(value: dict[str, Any]) -> dict[str, Any]:
    normalized_rules: list[dict[str, Any]] = []
    for raw_rule in value.get("rules", []):
        if not isinstance(raw_rule, dict):
            continue
        rule_type = raw_rule.get("type")
        if not isinstance(rule_type, str):
            continue
        rule: dict[str, Any] = {"type": rule_type}
        if rule_type == "pull_request":
            parameters = raw_rule.get("parameters")
            if isinstance(parameters, dict):
                rule["parameters"] = {
                    key: parameters.get(key)
                    for key in PULL_REQUEST_PARAMETERS
                }
                methods = rule["parameters"]["allowed_merge_methods"]
                if isinstance(methods, list):
                    rule["parameters"]["allowed_merge_methods"] = sorted(methods)
        normalized_rules.append(rule)
    normalized_rules.sort(key=lambda item: item["type"])

    bypass_actors = value.get("bypass_actors")
    if bypass_actors is None:
        normalized_bypass: Any = "__UNVERIFIED__"
    elif isinstance(bypass_actors, list):
        normalized_bypass = sorted(
            bypass_actors,
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    else:
        normalized_bypass = bypass_actors

    return {
        "name": value.get("name"),
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "bypass_actors": normalized_bypass,
        "conditions": value.get("conditions"),
        "rules": normalized_rules,
    }


def _normalized_effective_expected(ruleset: dict[str, Any]) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    for rule in ruleset.get("rules", []):
        if not isinstance(rule, dict) or rule.get("type") not in EFFECTIVE_RULE_TYPES:
            continue
        normalized = {"type": rule["type"]}
        if rule["type"] == "pull_request":
            normalized["parameters"] = {
                "required_approving_review_count": PULL_REQUEST_PARAMETERS[
                    "required_approving_review_count"
                ]
            }
        expected.append(normalized)
    return expected


def _normalize_effective_rule(value: dict[str, Any]) -> dict[str, Any]:
    rule_type = value.get("type")
    rule: dict[str, Any] = {"type": rule_type}

    if rule_type == "pull_request":
        parameters = value.get("parameters")
        if (
            isinstance(parameters, dict)
            and "required_approving_review_count" in parameters
        ):
            rule["parameters"] = {
                "required_approving_review_count": parameters[
                    "required_approving_review_count"
                ]
            }

    for key in (
        "ruleset_id",
        "ruleset_source_type",
        "ruleset_source",
    ):
        if key in value:
            rule[key] = value[key]

    return rule


def _find_effective_rules(
    values: list[Any], managed_ruleset_id: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], bool]:
    matching: list[dict[str, Any]] = []
    source_rules: list[dict[str, Any]] = []
    by_type: dict[str, list[dict[str, Any]]] = {
        rule_type: [] for rule_type in EFFECTIVE_RULE_TYPES
    }

    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise RepositoryPolicyError(
                f"unexpected branch effective rule at index {index}"
            )
        rule_type = item.get("type")
        if not isinstance(rule_type, str):
            raise RepositoryPolicyError(
                f"branch effective rule at index {index} is missing its type"
            )
        if rule_type not in EFFECTIVE_RULE_TYPES:
            continue
        normalized = _normalize_effective_rule(item)
        source_rules.append(normalized)
        by_type[rule_type].append(normalized)
        if (
            managed_ruleset_id is not None
            and normalized.get("ruleset_id") == managed_ruleset_id
        ):
            matching.append(normalized)

    source_rules.sort(key=lambda item: item["type"])
    matching.sort(key=lambda item: item["type"])

    drift: list[str] = []
    if managed_ruleset_id is None:
        drift.append(
            "managed ruleset id is missing, so effective rules cannot be attributed"
        )
        return source_rules, matching, drift, False

    existing_types = {rule["type"] for rule in matching}
    for rule_type in EFFECTIVE_RULE_TYPES:
        if rule_type not in existing_types:
            conflicting = by_type[rule_type]
            if conflicting:
                ids = sorted(
                    {
                        rule.get("ruleset_id")
                        for rule in conflicting
                        if isinstance(rule.get("ruleset_id"), int)
                    }
                )
                if ids:
                    drift.append(
                        f"{rule_type} rule is not enforced by managed ruleset "
                        f"{managed_ruleset_id}; found source ruleset IDs={ids}"
                    )
                else:
                    drift.append(
                        f"{rule_type} rule is missing for managed ruleset "
                        f"{managed_ruleset_id}"
                    )
            else:
                drift.append(
                    f"{rule_type} rule is missing for managed ruleset "
                    f"{managed_ruleset_id}"
                )

    for rule in matching:
        if rule["type"] != "pull_request":
            continue
        actual_parameters = rule.get("parameters")
        if not isinstance(actual_parameters, dict):
            continue
        if "required_approving_review_count" in actual_parameters:
            actual_count = actual_parameters.get("required_approving_review_count")
            if actual_count != PULL_REQUEST_PARAMETERS["required_approving_review_count"]:
                drift.append(
                    "effective pull_request rule reports "
                    f"required_approving_review_count={actual_count!r}; expected 0"
                )

    match = not drift
    return source_rules, matching, drift, match


def normalized_desired(policy: dict[str, Any]) -> dict[str, Any]:
    result = normalize_ruleset(desired_ruleset(policy))
    for rule in result["rules"]:
        if rule["type"] == "pull_request":
            rule["parameters"]["allowed_merge_methods"] = sorted(
                rule["parameters"]["allowed_merge_methods"]
            )
    return result


def find_managed_ruleset(
    root: Path,
    repository: str,
) -> tuple[int | None, dict[str, Any] | None]:
    values = gh_api(
        root,
        "GET",
        f"repos/{repository}/rulesets?includes_parents=false&per_page=100",
    )
    if not isinstance(values, list):
        raise RepositoryPolicyError("unexpected repository rulesets response")

    matches = [
        item
        for item in values
        if isinstance(item, dict)
        and item.get("name") == RULESET_NAME
        and item.get("source_type") == "Repository"
    ]
    if len(matches) > 1:
        raise RepositoryPolicyError(
            f"multiple repository rulesets named {RULESET_NAME!r} exist"
        )
    if not matches:
        return None, None

    ruleset_id = matches[0].get("id")
    if not isinstance(ruleset_id, int):
        raise RepositoryPolicyError("managed repository ruleset is missing its id")
    detail = gh_api(root, "GET", f"repos/{repository}/rulesets/{ruleset_id}")
    if not isinstance(detail, dict):
        raise RepositoryPolicyError("unexpected managed repository ruleset response")
    return ruleset_id, detail


def inspect(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    repository = current_repository(root)
    repository_data = gh_api(root, "GET", f"repos/{repository}")
    if not isinstance(repository_data, dict):
        raise RepositoryPolicyError("unexpected repository response")

    expected_branch = policy["default_branch"]
    actual_branch = repository_data.get("default_branch")
    expected_branch_exists = branch_exists(root, repository, expected_branch)

    ruleset_id, actual_ruleset = find_managed_ruleset(root, repository)
    expected_ruleset = normalized_desired(policy)
    normalized_actual = (
        None if actual_ruleset is None else normalize_ruleset(actual_ruleset)
    )
    configured_match = normalized_actual == expected_ruleset
    expected_effective_rules = _normalized_effective_expected(expected_ruleset)
    expected_effective_rules = sorted(
        expected_effective_rules, key=lambda rule: rule["type"]
    )

    effective_rules: list[dict[str, Any]]
    matching_effective_rules: list[dict[str, Any]]
    effective_drift: list[str]
    effective_match: bool

    if expected_branch_exists:
        branch_rules_value = gh_api(
            root,
            "GET",
            f"repos/{repository}/rules/branches/{expected_branch}",
        )
        if not isinstance(branch_rules_value, list):
            raise RepositoryPolicyError("unexpected branch effective rules response")
        (
            effective_rules,
            matching_effective_rules,
            effective_drift,
            effective_match,
        ) = _find_effective_rules(branch_rules_value, ruleset_id)
    else:
        effective_rules = []
        matching_effective_rules = []
        effective_drift = [
            f"effective rules for {expected_branch!r} cannot be verified because "
            "the branch does not exist"
        ]
        effective_match = False

    drift: list[str] = []
    if actual_branch != expected_branch:
        drift.append(
            f"default branch is {actual_branch!r}; expected {expected_branch!r}"
        )
    if actual_branch != expected_branch and not expected_branch_exists:
        drift.append(
            f"required branch {expected_branch!r} does not exist; "
            "create or rename it before applying policy"
        )
    if actual_ruleset is None:
        drift.append(f"missing ruleset {RULESET_NAME!r}")
    elif not configured_match:
        drift.append(f"ruleset {RULESET_NAME!r} differs from policy")

    if not effective_match:
        if configured_match:
            if effective_drift:
                drift.append(
                    "configured ruleset matches policy, but it is not effective on "
                    f"main: {'; '.join(effective_drift)}"
                )
            else:
                drift.append(
                    "configured ruleset matches policy, but it is not effective on "
                    "main"
                )
        else:
            if expected_branch_exists:
                drift.append(
                    "effective rules on main do not match expected policy: "
                    + "; ".join(effective_drift)
                )
            else:
                drift.append(
                    "effective rules on main are not verifiable because main is missing"
                )

    return {
        "repository": repository,
        "visibility": repository_data.get("visibility"),
        "policyVersion": policy["version"],
        "defaultBranch": {
            "expected": expected_branch,
            "actual": actual_branch,
            "expectedBranchExists": expected_branch_exists,
            "match": actual_branch == expected_branch,
        },
        "ruleset": {
            "name": RULESET_NAME,
            "id": ruleset_id,
            "present": actual_ruleset is not None,
            "match": configured_match,
            "actual": normalized_actual,
            "expected": expected_ruleset,
        },
        "effectiveRules": {
            "expected": expected_effective_rules,
            "actual": matching_effective_rules,
            "match": effective_match,
            "managedRulesetId": ruleset_id,
            "drift": effective_drift,
            "rulesFromBranch": effective_rules,
        },
        "drift": drift,
    }


def command_check(root: Path) -> int:
    policy = load_policy(root)
    result = inspect(root, policy)
    result["status"] = "PASS" if not result["drift"] else "DRIFT"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result["drift"] else 1


def command_apply(root: Path) -> int:
    if task_worktree(root):
        raise RepositoryPolicyError(
            "repository policy mutation is forbidden from a Task worktree"
        )

    policy = load_policy(root)
    before = inspect(root, policy)
    repository = before["repository"]
    expected_branch = policy["default_branch"]

    if not before["defaultBranch"]["expectedBranchExists"]:
        raise RepositoryPolicyError(
            f"cannot apply repository policy: branch {expected_branch!r} does not exist"
        )

    if before["defaultBranch"]["actual"] != expected_branch:
        gh_api(
            root,
            "PATCH",
            f"repos/{repository}",
            body={"default_branch": expected_branch},
        )

    ruleset_id = before["ruleset"]["id"]
    if ruleset_id is None:
        gh_api(
            root,
            "POST",
            f"repos/{repository}/rulesets",
            body=desired_ruleset(policy),
        )
    elif not before["ruleset"]["match"]:
        gh_api(
            root,
            "PUT",
            f"repos/{repository}/rulesets/{ruleset_id}",
            body=desired_ruleset(policy),
        )

    after = inspect(root, policy)
    if after["drift"]:
        raise RepositoryPolicyError(
            "repository policy apply completed but verification still reports drift: "
            + "; ".join(after["drift"])
        )

    after["status"] = "PASS"
    after["changed"] = bool(before["drift"])
    print(json.dumps(after, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Check and apply the Agent repository GitHub policy"
    )
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("apply")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        root = repository_root()
        if args.command == "check":
            return command_check(root)
        if args.command == "apply":
            return command_apply(root)
        raise AssertionError(args.command)
    except RepositoryPolicyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
