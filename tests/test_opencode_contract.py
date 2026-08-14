from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import re
import unittest
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core"
AGENTS = CORE / ".opencode" / "agents"

LEAF_PRIMARY_AGENTS = (
    "general",
    "explore",
    "verifier",
    "reviewer",
    "investigator",
    "security-reviewer",
    "scout",
    "architect",
)
TASK_ORCHESTRATOR_LEAVES = (
    "general",
    "explore",
    "verifier",
    "reviewer",
    "investigator",
    "security-reviewer",
    "scout",
)
LEAF_STATUS_SET = {"COMPLETED", "BLOCKED", "NEEDS_APPROVAL", "NEEDS_DECISION"}
READ_ONLY_GIT_COMMANDS = {
    "git status",
    "git status *",
    "git diff",
    "git diff *",
    "git log",
    "git log *",
    "git show *",
    "git blame *",
    "git grep *",
    "git rev-parse *",
    "git ls-files *",
    "git merge-base *",
    "git cat-file *",
    "git branch --list *",
    "git remote -v",
    "git worktree list *",
}
PROJECT_CHECK_COMMANDS = {
    "just project::doctor",
    "just project::eval",
    "just project::format-check",
    "just project::lint",
    "just project::test",
    "just project::build",
    "just project::check",
}
LEAF_ALLOWED_BASH = {
    "general": READ_ONLY_GIT_COMMANDS | PROJECT_CHECK_COMMANDS,
    "explore": READ_ONLY_GIT_COMMANDS,
    "verifier": {"git status", "git status *", "git diff", "git diff *"}
    | PROJECT_CHECK_COMMANDS,
    "reviewer": set(),
    "investigator": READ_ONLY_GIT_COMMANDS | PROJECT_CHECK_COMMANDS,
    "security-reviewer": READ_ONLY_GIT_COMMANDS,
    "scout": set(),
    "architect": READ_ONLY_GIT_COMMANDS,
}


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1]) and value[0] in {'"', "'"}):
        return value[1:-1]
    return value


def _partition_mapping_line(line: str) -> tuple[str, str] | None:
    match = re.match(r'^(?:"([^"]+)"|\'([^\']+)\'|([^:]+)):\s*(.*)$', line)
    if match is None:
        return None
    key = next(group for group in match.groups()[:3] if group is not None)
    return key.strip(), match.group(4).strip()


def _parse_yamlish_mapping(
    lines: list[str], start: int, base_indent: int
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue

        indent = _line_indent(raw)
        if indent < base_indent:
            break
        if indent > base_indent:
            i += 1
            continue

        stripped = raw.strip()
        entry = _partition_mapping_line(stripped)
        if entry is None:
            i += 1
            continue

        key, value = entry

        if value:
            mapping[key] = _unquote(value)
            i += 1
            continue

        # Nested map; advance to next line and parse any deeper lines regardless of spacing.
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines) or _line_indent(lines[i]) <= base_indent:
            mapping[key] = {}
            continue

        child_indent = _line_indent(lines[i])
        nested, i = _parse_yamlish_mapping(lines, i, child_indent)
        mapping[key] = nested

    return mapping, i


def parse_permission_block(frontmatter_text: str) -> dict[str, Any]:
    lines = frontmatter_text.splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^permission:\s*$", line.strip()):
            base_indent = _line_indent(line) + 2
            parsed, _ = _parse_yamlish_mapping(lines, idx + 1, base_indent)
            return parsed
    raise AssertionError("missing permission: frontmatter block")


def _iter_permission_values(permission_value: Any) -> Iterable[str]:
    if isinstance(permission_value, dict):
        for value in permission_value.values():
            yield from _iter_permission_values(value)
    else:
        if isinstance(permission_value, str):
            yield permission_value


def permission_entries_are_scalar_lines(permission: dict[str, Any]) -> bool:
    return all(isinstance(value, (str, dict)) for value in permission.values())


def permission_for(agent: str) -> dict[str, Any]:
    return parse_permission_block(frontmatter(AGENTS / f"{agent}.md"))


def body_text(agent: str) -> str:
    text = (AGENTS / f"{agent}.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n.*?\n---\n(.*)", text, flags=re.S)
    if not match:
        raise AssertionError(f"missing frontmatter body: {agent}")
    return match.group(1)


def status_report_lines(text: str) -> set[str]:
    return set(re.findall(r"\b(?:COMPLETED|BLOCKED|NEEDS_APPROVAL|NEEDS_DECISION)\b", text))


def assert_prompt_contract_for_leaf_statuses(test_case: unittest.TestCase, body: str, name: str) -> None:
    test_case.assertEqual(status_report_lines(body), LEAF_STATUS_SET, name)
    lower = body.lower()
    test_case.assertIn("non-interactive", lower, name)
    test_case.assertIn("start the final response with exactly one `status:", lower, name)
    test_case.assertIn("do not attempt denied operations", lower, name)
    test_case.assertIn("ask the user", lower, name)
    test_case.assertIn("call `question`", lower, name)
    test_case.assertIn("claim any unexecuted command as executed/passed", lower, name)
    for field in (
        "denied_operation",
        "why_needed",
        "supporting_evidence",
        "expected_effect",
        "consequence_if_denied",
        "work_unit_state",
        "safe_continuation_point",
        "safe_alternatives",
    ):
        test_case.assertIn(field, lower, name)
    test_case.assertIn("ambiguity, options with tradeoffs, and recommendation", lower, name)


def frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    return match.group(1)


class OpenCodeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((CORE / "opencode.json").read_text(encoding="utf-8"))

    def test_depth_and_default_agent(self) -> None:
        self.assertEqual(self.config["default_agent"], "build")
        self.assertEqual(self.config["subagent_depth"], 2)

    def test_external_directory_boundary(self) -> None:
        rules = self.config["permission"]["external_directory"]
        self.assertEqual(rules["*"], "deny")
        self.assertEqual(rules["/tmp/opencode"], "ask")
        self.assertEqual(rules["/tmp/opencode/**"], "ask")

    def test_git_and_github_writes_require_just_api(self) -> None:
        bash = self.config["permission"]["bash"]
        self.assertEqual(bash["git push *"], "deny")
        self.assertEqual(bash["git commit *"], "deny")
        self.assertEqual(bash["gh pr create *"], "deny")
        self.assertEqual(bash["gh pr edit *"], "deny")
        self.assertEqual(bash["gh pr ready *"], "deny")
        self.assertEqual(bash["gh pr merge *"], "deny")
        self.assertEqual(bash["just agent::commit *"], "allow")
        self.assertEqual(bash["just agent::pr-create *"], "allow")
        self.assertEqual(bash["just agent::pr-edit *"], "allow")
        self.assertEqual(bash["just agent::pr-ready *"], "allow")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["just integrate::merge *"], "ask")

    def test_automation_core_is_not_editable(self) -> None:
        edit = self.config["permission"]["edit"]
        for path in (
            "opencode.json",
            "AGENTS.md",
            "Justfile",
            ".opencode/**",
            ".automation/**",
            ".github/workflows/**",
            ".task-state/**",
        ):
            self.assertEqual(edit[path], "deny", path)

    def test_model_assignment_is_exact(self) -> None:
        expected = {
            "build.md": "openai/gpt-5.6-sol",
            "plan.md": "openai/gpt-5.6-sol",
            "task-orchestrator.md": "openai/gpt-5.3-codex-spark",
            "general.md": "openai/gpt-5.3-codex-spark",
            "explore.md": "openai/gpt-5.3-codex-spark",
            "verifier.md": "openai/gpt-5.3-codex-spark",
            "reviewer.md": "openai/gpt-5.6-terra",
            "investigator.md": "openai/gpt-5.6-terra",
            "security-reviewer.md": "openai/gpt-5.6-terra",
            "scout.md": "openai/gpt-5.3-codex-spark",
            "architect.md": "openai/gpt-5.6-sol",
        }
        for filename, model in expected.items():
            self.assertIn(f"model: {model}", frontmatter(AGENTS / filename), filename)

    def test_spark_primary_agents(self) -> None:
        spark_primaries = {
            path.name
            for path in AGENTS.glob("*.md")
            if not path.name.endswith("-fallback.md")
            and "model: openai/gpt-5.3-codex-spark" in frontmatter(path)
        }
        self.assertEqual(spark_primaries, {"task-orchestrator.md", "general.md", "explore.md", "verifier.md", "scout.md"})

    def test_spark_leaf_fallback_agents(self) -> None:
        expected_luna_fallback = {
            "general-fallback.md",
            "explore-fallback.md",
            "verifier-fallback.md",
            "scout-fallback.md",
        }
        for filename in expected_luna_fallback:
            self.assertIn("model: openai/gpt-5.6-luna", frontmatter(AGENTS / filename), filename)

    def test_fallback_model_assignment_is_exact(self) -> None:
        expected = {
            "build-fallback.md": "openai/gpt-5.3-codex-spark",
            "architect-fallback.md": "openai/gpt-5.3-codex-spark",
            "task-orchestrator-fallback.md": "openai/gpt-5.6-sol",
            "general-fallback.md": "openai/gpt-5.6-luna",
            "explore-fallback.md": "openai/gpt-5.6-luna",
            "verifier-fallback.md": "openai/gpt-5.6-luna",
            "reviewer-fallback.md": "openai/gpt-5.3-codex-spark",
            "investigator-fallback.md": "openai/gpt-5.3-codex-spark",
            "security-reviewer-fallback.md": "openai/gpt-5.3-codex-spark",
            "scout-fallback.md": "openai/gpt-5.6-luna",
        }
        for filename, model in expected.items():
            self.assertIn(f"model: {model}", frontmatter(AGENTS / filename), filename)


    def test_plan_agent_repository_local_read_only_contract(self) -> None:
        front = frontmatter(AGENTS / "plan.md")
        body = body_text("plan")
        permission = permission_for("plan")

        self.assertIn("mode: primary", front)
        self.assertIn("model: openai/gpt-5.6-sol", front)
        self.assertEqual(permission.get("edit"), "deny")
        self.assertEqual(permission.get("question"), "allow")
        self.assertEqual(permission.get("skill"), "allow")
        self.assertEqual(permission.get("external_directory"), "deny")
        self.assertEqual(permission.get("doom_loop"), "deny")
        self.assertEqual(permission.get("webfetch"), "deny")
        self.assertEqual(permission.get("websearch"), "deny")
        self.assertEqual(permission.get("bash"), "deny")

        task = permission.get("task")
        self.assertIsInstance(task, dict)
        allowed = {name for name, action in task.items() if action == "allow"}
        self.assertEqual(
            allowed,
            {
                "explore",
                "explore-fallback",
                "architect",
                "architect-fallback",
                "reviewer",
                "reviewer-fallback",
                "security-reviewer",
                "security-reviewer-fallback",
            },
        )
        self.assertEqual(task.get("*"), "deny")
        for forbidden in (
            "general",
            "general-fallback",
            "verifier",
            "verifier-fallback",
            "investigator",
            "investigator-fallback",
            "task-orchestrator",
            "task-orchestrator-fallback",
            "scout",
            "scout-fallback",
        ):
            self.assertEqual(task.get(forbidden), "deny", forbidden)

        lower = body.lower()
        for phrase in (
            "planning_initialization_handoff",
            "execution_prerequisites",
            "verification_handoff",
            "unexecuted",
            "never pass",
            "do not run `just agent::doctor`",
            "do not run `just agent::context`",
            "do not run `just project::doctor`",
            "do not edit files",
            "do not delegate to `general`",
            "do not delegate to `verifier`",
            "do not delegate to `investigator`",
            "do not delegate to `task-orchestrator`",
        ):
            self.assertIn(phrase, lower)


    def test_opencode_debug_agent_plan_effective_policy_when_cli_available(self) -> None:
        opencode = shutil.which("opencode")
        if opencode is None:
            self.skipTest("opencode CLI is not available")

        source_project = ROOT / "templates" / "agent-base"
        with tempfile.TemporaryDirectory(prefix="templates-56-opencode-") as directory:
            project = Path(directory) / "agent-base"
            shutil.copytree(source_project, project)
            config = Path(directory) / "opencode"
            agents = config / "agents"
            agents.mkdir(parents=True)
            (config / "opencode.json").write_text(
                json.dumps(
                    {
                        "permission": {
                            "task": {"*": "allow", "general": "allow", "verifier": "allow"},
                            "bash": {"*": "allow"},
                            "edit": "allow",
                            "question": "deny",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (agents / "plan.md").write_text(
                """---
description: deliberately permissive global plan
mode: primary
model: openai/gpt-5.6-sol
permission:
  edit: allow
  question: deny
  task:
    "*": allow
    general: allow
    verifier: allow
  bash:
    "*": allow
---

global permissive plan
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = directory
            env["OPENCODE_CONFIG_HOME"] = str(config)
            result = subprocess.run(
                [opencode, "debug", "agent", "plan", "--pure"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        data = json.loads(result.stdout)
        permissions = data["permission"]

        def last_action(permission: str, pattern: str = "*") -> str:
            matches = [
                item["action"]
                for item in permissions
                if item["permission"] == permission and item.get("pattern") == pattern
            ]
            self.assertTrue(matches, f"missing {permission}:{pattern}")
            return matches[-1]

        self.assertEqual(data["description"], "Repository-local read-only planning agent")
        self.assertEqual(data["mode"], "primary")
        self.assertEqual(data["model"], {"providerID": "openai", "modelID": "gpt-5.6-sol"})
        self.assertEqual(last_action("edit"), "deny")
        self.assertEqual(last_action("question"), "allow")
        self.assertEqual(last_action("bash"), "deny")
        self.assertEqual(last_action("task"), "deny")
        for allowed in (
            "explore",
            "explore-fallback",
            "architect",
            "architect-fallback",
            "reviewer",
            "reviewer-fallback",
            "security-reviewer",
            "security-reviewer-fallback",
        ):
            self.assertEqual(last_action("task", allowed), "allow", allowed)
        for denied in (
            "general",
            "general-fallback",
            "verifier",
            "verifier-fallback",
            "investigator",
            "investigator-fallback",
            "task-orchestrator",
            "task-orchestrator-fallback",
            "scout",
            "scout-fallback",
        ):
            self.assertEqual(last_action("task", denied), "deny", denied)

    def test_leaf_agents_cannot_delegate(self) -> None:
        for filename in [f"{leaf}.md" for leaf in LEAF_PRIMARY_AGENTS]:
            self.assertIn("task: deny", frontmatter(AGENTS / filename), filename)

    def test_leaf_agents_have_authority_contract(self) -> None:
        for leaf in LEAF_PRIMARY_AGENTS:
            primary = permission_for(leaf)
            fallback = permission_for(f"{leaf}-fallback")

            self.assertTrue(permission_entries_are_scalar_lines(primary), leaf)
            self.assertTrue(permission_entries_are_scalar_lines(fallback), f"{leaf}-fallback")

            self.assertEqual(primary.get("task"), "deny", leaf)
            self.assertEqual(fallback.get("task"), "deny", f"{leaf}-fallback")

            self.assertEqual(primary.get("question"), "deny", leaf)
            self.assertEqual(fallback.get("question"), "deny", f"{leaf}-fallback")

            self.assertEqual(primary.get("external_directory"), "deny", leaf)
            self.assertEqual(fallback.get("external_directory"), "deny", f"{leaf}-fallback")

            self.assertEqual(primary.get("doom_loop"), "deny", leaf)
            self.assertEqual(fallback.get("doom_loop"), "deny", f"{leaf}-fallback")

            for action in _iter_permission_values(primary):
                self.assertNotEqual(action, "ask", leaf)
            for action in _iter_permission_values(fallback):
                self.assertNotEqual(action, "ask", f"{leaf}-fallback")

            if leaf == "scout":
                self.assertEqual(primary.get("webfetch"), "allow", leaf)
                self.assertEqual(primary.get("websearch"), "allow", leaf)
                self.assertEqual(fallback.get("webfetch"), "allow", f"{leaf}-fallback")
                self.assertEqual(fallback.get("websearch"), "allow", f"{leaf}-fallback")
            else:
                self.assertEqual(primary.get("webfetch"), "deny", leaf)
                self.assertEqual(primary.get("websearch"), "deny", leaf)
                self.assertEqual(fallback.get("webfetch"), "deny", f"{leaf}-fallback")
                self.assertEqual(fallback.get("websearch"), "deny", f"{leaf}-fallback")

            if leaf != "general":
                self.assertEqual(primary.get("edit"), "deny", leaf)
                self.assertEqual(fallback.get("edit"), "deny", f"{leaf}-fallback")

            primary_bash = primary.get("bash", {})
            fallback_bash = fallback.get("bash", {})
            self.assertIsInstance(primary_bash, dict, leaf)
            self.assertIsInstance(fallback_bash, dict, f"{leaf}-fallback")

            self.assertEqual(primary_bash.get("*"), "deny", leaf)
            self.assertEqual(fallback_bash.get("*"), "deny", f"{leaf}-fallback")

            primary_allowed = {cmd for cmd, action in primary_bash.items() if action == "allow"}
            fallback_allowed = {cmd for cmd, action in fallback_bash.items() if action == "allow"}
            self.assertEqual(primary_allowed, LEAF_ALLOWED_BASH[leaf], leaf)
            self.assertEqual(fallback_allowed, LEAF_ALLOWED_BASH[leaf], f"{leaf}-fallback")

            for command, action in primary_bash.items():
                if action == "allow":
                    self.assertNotRegex(
                        command,
                        r"(agent::task-start|agent::state-set|agent::batch-plan|agent::commit|agent::push|agent::pr-"
                        r"create|agent::pr-edit|agent::pr-ready|agent::cleanup|integrate::check|integrate::merge|"
                        r"integrate::status|project::commit|project::publish|project::release)",
                        leaf,
                    )
            for command, action in fallback_bash.items():
                if action == "allow":
                    self.assertNotRegex(
                        command,
                        r"(agent::task-start|agent::state-set|agent::batch-plan|agent::commit|agent::push|agent::pr-"
                        r"create|agent::pr-edit|agent::pr-ready|agent::cleanup|integrate::check|integrate::merge|"
                        r"integrate::status|project::commit|project::publish|project::release)",
                        f"{leaf}-fallback",
                    )

            # Primary/fallback authority parity; metadata/model differences are not part of this assertion.
            self.assertEqual(primary, fallback, leaf)

    def test_leaf_prompts_expose_completion_status_contract(self) -> None:
        for leaf in LEAF_PRIMARY_AGENTS:
            for agent_name in (leaf, f"{leaf}-fallback"):
                assert_prompt_contract_for_leaf_statuses(self, body_text(agent_name), agent_name)

    def test_task_orchestrator_fallback_policy_contracts(self) -> None:
        primary_text = body_text("task-orchestrator").lower()
        fallback_text = body_text("task-orchestrator-fallback").lower()
        self.assertIn("retry the identical work unit once", primary_text)
        self.assertIn("do not fallback for authentication", primary_text)
        self.assertIn("authentication, permission", primary_text)
        self.assertIn("when the chain is exhausted, set the task blocked", primary_text)
        self.assertIn("record the failed model, classified reason, selected fallback model", primary_text)

        self.assertIn("same authority and constraints", fallback_text)
        self.assertIn("explicit model fallback policy", fallback_text)
        self.assertIn("classified usage/quota/rate-limit failure", fallback_text)

        primary_permissions = permission_for("task-orchestrator")
        fallback_permissions = permission_for("task-orchestrator-fallback")
        self.assertEqual(primary_permissions, fallback_permissions)
        self.assertEqual(primary_permissions.get("question"), "allow")

        self.assertIn("blocked", primary_text)
        self.assertIn("continue", primary_text)
        self.assertTrue(any(term in primary_text for term in ("rejection", "reject")), "task-orchestrator should define rejection behavior")
        self.assertTrue(any(term in primary_text for term in ("continue", "continuing", "continuation")), "task-orchestrator should define continuation behavior")
        self.assertTrue(
            "launder" in primary_text,
            "task-orchestrator should contain anti-laundering language",
        )
        for text, name in (
            (primary_text, "task-orchestrator"),
            (fallback_text, "task-orchestrator-fallback"),
        ):
            self.assertIn("needs_decision", text, name)
            self.assertIn("task contract", text, name)
            self.assertIn("question", text, name)
            self.assertIn("options", text, name)
            self.assertIn("tradeoffs", text, name)
            self.assertIn("recommendation", text, name)
            self.assertIn("user-rejected", text, name)
            self.assertIn("final for that exact operation", text, name)
            self.assertIn("never retry, rephrase, re-delegate, or substitute", text, name)
            self.assertNotIn("elevate to depth 0", text, name)

    def test_task_orchestrator_call_graph_is_non_cyclic(self) -> None:
        task_permissions = permission_for("task-orchestrator").get("task", {})
        self.assertIsInstance(task_permissions, dict)
        self.assertEqual(task_permissions.get("*"), "deny")
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            self.assertEqual(task_permissions.get(leaf), "allow", leaf)
            self.assertEqual(task_permissions.get(f"{leaf}-fallback"), "allow", f"{leaf}-fallback")

        self.assertNotIn("task-orchestrator", task_permissions)
        self.assertNotIn("task-orchestrator-fallback", task_permissions)
        expected_task_targets = {"*"}
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            expected_task_targets.add(leaf)
            expected_task_targets.add(f"{leaf}-fallback")
        self.assertEqual(set(task_permissions), expected_task_targets)

    def test_recovery_contract_and_authority_parity(self) -> None:
        self.assertEqual(permission_for("build"), permission_for("build-fallback"))
        primary = permission_for("task-orchestrator")
        fallback = permission_for("task-orchestrator-fallback")
        self.assertEqual(primary, fallback)
        for command in (
            "just agent::recovery-status *",
            "just agent::recovery-route *",
            "just agent::recovery-record *",
            "just agent::work-unit-register *",
            "just agent::work-unit-status *",
            "just agent::work-unit-state-set *",
        ):
            self.assertEqual(primary["bash"].get(command), "allow", command)
        for command in ("just agent::recovery-start *", "just agent::recovery-clear *"):
            self.assertEqual(primary["bash"].get(command), "deny", command)

        recover = (CORE / ".opencode" / "commands" / "task-recover.md").read_text()
        self.assertIn("task-recovery", recover)
        self.assertIn("$ARGUMENTS", recover)
        self.assertNotIn("task-orchestrator", recover.lower())
        skill = (CORE / ".opencode" / "skills" / "task-recovery" / "SKILL.md").read_text().lower()
        for phrase in (
            "recovery-start",
            "recovery-status",
            "recovery-route",
            "recovery-record",
            "same task/worktree",
            "work unit semantic/id",
            "never launch an unavailable-family primary first",
            "integration-pending",
            "blocked",
        ):
            self.assertIn(phrase, skill)
        self.assertLess(skill.index("work-unit-status"), skill.index("next call `recovery-route`"))
        self.assertLess(skill.index("next call `recovery-route`"), skill.index("may the orchestrator delegate"))
        fallback_body = body_text("task-orchestrator-fallback").lower()
        self.assertIn("before every leaf delegation", fallback_body)
        self.assertIn("never launch the unavailable-family primary first", fallback_body)
        self.assertIn("permission-launder", fallback_body)


if __name__ == "__main__":
    unittest.main()
