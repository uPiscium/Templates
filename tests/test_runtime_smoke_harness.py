from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "runtime" / "runtime_smoke.py"
SPEC = importlib.util.spec_from_file_location("runtime_smoke", RUNNER)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSmokeHarnessTest(unittest.TestCase):
    def test_runtime_workspace_is_git_ignored_and_routed_through_just(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.runtime-smoke/", ignore)
        justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
        self.assertIn("mod runtime 'just/runtime.just'", justfile)
        module = (ROOT / "just" / "runtime.just").read_text(encoding="utf-8")
        for recipe in (
            "prepare issue='issue-41' template='agent-python'",
            "diagnose-child-stall issue='issue-41'",
            "smoke-depth2 issue='issue-41'",
            "smoke-escalation issue='issue-41'",
            "smoke-escalation-reject issue='issue-41'",
            "validate-escalation issue='issue-41'",
            "smoke-leaf-completed issue='issue-41'",
            "smoke-leaf-blocked issue='issue-41'",
            "validate-leaf-contract issue='issue-41'",
            "direct-leaf issue='issue-41'",
            "export-session session issue='issue-41'",
        ):
            self.assertIn(recipe, module)

    def test_workspace_names_cannot_escape_runtime_root(self) -> None:
        with self.assertRaises(runtime.RuntimeSmokeError):
            runtime.workspace("../outside")
        with self.assertRaises(runtime.RuntimeSmokeError):
            runtime.validate_name("bad/name", "name")
        resolved = runtime.workspace("issue-41")
        self.assertEqual(resolved.parent, ROOT / ".runtime-smoke")

    def test_deterministic_runtime_tasks_are_defined(self) -> None:
        tasks = {(task, slug) for task, slug, *_ in runtime.TASK_DEFINITIONS}
        self.assertEqual(
            tasks,
            {
                ("SMOKE-LEAF-COMPLETED", "depth2-leaf-completed"),
                ("SMOKE-LEAF-BLOCKED", "depth2-leaf-blocked"),
                ("SMOKE-CONTROL", "ask-free-control"),
                ("SMOKE-ASK", "depth2-ask"),
                ("SMOKE-ESCALATION", "leaf-escalation"),
                ("SMOKE-ESCALATION-PERMISSION", "leaf-escalation-permission"),
            },
        )

        completed = next(
            definition for definition in runtime.TASK_DEFINITIONS
            if definition[0] == "SMOKE-LEAF-COMPLETED"
        )
        completed_text = " ".join(completed[3] + completed[4])
        self.assertIn("git status --short", completed_text)
        self.assertIn("status: COMPLETED", completed_text)
        self.assertIn("agent::doctor", completed_text)
        self.assertIn("project::doctor", completed_text)

        blocked = next(
            definition for definition in runtime.TASK_DEFINITIONS
            if definition[0] == "SMOKE-LEAF-BLOCKED"
        )
        self.assertIn("status: BLOCKED", " ".join(blocked[3] + blocked[4]))

        smoke_ask = next(
            definition
            for definition in runtime.TASK_DEFINITIONS
            if definition[0] == "SMOKE-ASK"
        )
        scope = " ".join(smoke_ask[3])
        self.assertIn("must pass this exact Work Unit instruction", scope)
        self.assertIn("do not call `question` or any other tool first", scope)
        self.assertIn("printf 'depth2-ask-approved\\n'", scope)
        self.assertIn("printf 'depth2-ask-rejected\\n'", scope)
        self.assertNotIn("printf 'leaf-escalation-approved\\n'", scope)
        self.assertNotIn("git push origin HEAD:main", scope)

        smoke_escalation = next(
            definition
            for definition in runtime.TASK_DEFINITIONS
            if definition[0] == "SMOKE-ESCALATION"
        )
        escalation_scope = " ".join(smoke_escalation[3])
        escalation_acceptance = " ".join(smoke_escalation[4])
        self.assertIn("NEEDS_APPROVAL", escalation_scope)
        self.assertIn("leaf-escalation-approved\\n", escalation_scope)
        self.assertIn("git push origin HEAD:main", escalation_scope)
        self.assertNotIn("printf 'depth2-ask-approved\\n'", escalation_scope)
        self.assertNotIn("printf 'depth2-ask-rejected\\n'", escalation_scope)
        self.assertIn("exact", escalation_scope)
        self.assertIn("exact four decision steps", escalation_scope)
        self.assertIn("must not call Bash", escalation_scope)
        self.assertIn("immediately call Bash", escalation_scope)
        self.assertIn("no other tool first", escalation_scope)
        self.assertIn("internal Depth-1 rejection", escalation_scope)
        self.assertIn("Do not call Bash or create a permission request", escalation_scope)
        self.assertIn("ordinary Task closeout", escalation_scope)
        self.assertIn("Bounded read-only policy inspection", escalation_scope)
        self.assertIn("NEEDS_APPROVAL", escalation_acceptance)
        self.assertIn("Depth-1", escalation_acceptance)
        self.assertIn("No denied command execution", escalation_acceptance)
        self.assertIn("four decision steps occur in order", escalation_acceptance)
        self.assertIn("false", escalation_acceptance)

        smoke_reject = next(
            definition
            for definition in runtime.TASK_DEFINITIONS
            if definition[0] == "SMOKE-ESCALATION-PERMISSION"
        )
        reject_scope = " ".join(smoke_reject[3])
        reject_acceptance = " ".join(smoke_reject[4])
        self.assertIn("leaf-escalation-user-rejected", reject_scope)
        self.assertIn("actual Main TUI permission result", reject_scope)
        self.assertIn("without predicting it", reject_scope)
        self.assertIn("first tool call after the Leaf return", reject_scope)
        self.assertIn("do not retry", reject_scope)
        self.assertIn("Task Orchestrator session", reject_acceptance)
        self.assertIn("command does not execute", reject_acceptance)

    def test_task_contract_replaces_unresolved_template_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task.md"
            path.write_text(
                "# TEST\n\n## Identity\n\n"
                "- Task ID: TEST\n"
                "- Branch: task/TEST-smoke\n"
                f"- Worktree: {Path(temporary)}\n"
                "- Base branch: main\n"
                "- Base revision: deadbeef\n\n"
                "## Purpose\n\nTBD\n",
                encoding="utf-8",
            )
            runtime.write_contract(
                path,
                purpose="Concrete runtime diagnostic.",
                scope=["One bounded action."],
                acceptance=["Evidence is recorded."],
                test_plan=["Run the bounded control."],
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("TBD", text)
            self.assertIn("## Stop conditions", text)
            self.assertIn("- Status: initialized", text)
            self.assertIn("- [ ] Evidence is recorded.", text)

    def test_validate_escalation_requires_ordered_noninteractive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "issue"
            logs = base / "logs"
            state = (
                base
                / "smoke-repo"
                / ".worktrees"
                / "SMOKE-ESCALATION-leaf-escalation"
                / ".task-state"
                / "task.md"
            )
            logs.mkdir(parents=True)
            state.parent.mkdir(parents=True)
            log = logs / "opencode-leaf-escalation-20260101.log"
            reject_log = logs / "opencode-leaf-escalation-reject-test.log"
            valid_lines = [
                "message=created id=to parentID=main agent=task-orchestrator",
                "message=created id=leaf1 parentID=to title=step1 agent=general",
                'message="exiting loop" session.id=leaf1',
                "message=process session.id=to messageID=approval-message",
                "message=asking permission=bash patterns=leaf-escalation-approved",
                "message=created id=leaf2 parentID=to title=step3 agent=general",
                'message="exiting loop" session.id=leaf2',
            ]
            valid_reject_lines = [
                "message=created id=reject-to parentID=main agent=task-orchestrator",
                "message=created id=reject-leaf parentID=reject-to title=user-reject agent=general",
                'message="exiting loop" session.id=reject-leaf',
                "message=process session.id=reject-to messageID=reject-message",
                "message=asking permission=bash patterns=leaf-escalation-user-rejected",
                "message=process session.id=main",
            ]
            log.write_text("\n".join(valid_lines) + "\n", encoding="utf-8")
            reject_log.write_text(
                "\n".join(valid_reject_lines) + "\n", encoding="utf-8"
            )
            state.write_text(
                "- Leaf general (Step 1): request -> `NEEDS_APPROVAL` (non-executed)\n"
                "- Bash (Depth-1): executed `printf 'leaf-escalation-approved\\n'` after approval.\n"
                "- Leaf general (Step 3): request -> `NEEDS_APPROVAL` (non-executed)\n"
                "- Step 4 Depth-1 decision: rejected `git push origin HEAD:main` without Bash or permission request.\n"
                "- output `leaf-escalation-approved`\n",
                encoding="utf-8",
            )
            original_workspace = runtime.workspace
            original_run_capture = runtime.run_capture
            rejection_export = {
                "messages": [
                    {
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "bash",
                                "state": {
                                    "status": "error",
                                    "error": "The user rejected permission to use this specific tool call.",
                                },
                                "messageID": "reject-message",
                            }
                        ]
                    }
                ]
            }
            runtime.workspace = lambda _issue: base
            runtime.run_capture = lambda _command, *, cwd: json.dumps(
                rejection_export
            )
            try:
                result = runtime.validate_escalation("test")
                self.assertEqual(result["status"], "PASS")
                self.assertEqual(result["approvedAskCount"], 1)
                self.assertEqual(result["approvedAskOrigin"], "to")
                self.assertEqual(result["rejectedAskOrigin"], "reject-to")
                self.assertEqual(result["rejectionReturnedToMainSession"], "main")
                self.assertEqual(result["rejectionToolStatus"], "error")
                self.assertIn(
                    "user rejected permission",
                    result["rejectionToolError"].lower(),
                )

                reject_log.write_text(
                    "\n".join(
                        [
                            *valid_reject_lines[:3],
                            "message=process session.id=wrong-origin messageID=reject-message",
                            valid_reject_lines[4],
                            valid_reject_lines[5],
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    runtime.RuntimeSmokeError,
                    "rejected Ask did not originate from the Task Orchestrator session",
                ):
                    runtime.validate_escalation("test")
                reject_log.write_text(
                    "\n".join(valid_reject_lines) + "\n", encoding="utf-8"
                )

                reject_log.write_text(
                    "\n".join([*valid_reject_lines[:-1], "message=process session.id=other"])
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    runtime.RuntimeSmokeError,
                    "rejected Ask did not return control to the parent Main session",
                ):
                    runtime.validate_escalation("test")
                reject_log.write_text(
                    "\n".join(valid_reject_lines) + "\n", encoding="utf-8"
                )

                rejection_export["messages"][0]["parts"][0]["state"] = {
                    "status": "completed",
                    "output": "leaf-escalation-user-rejected",
                }
                with self.assertRaisesRegex(
                    runtime.RuntimeSmokeError,
                    "does not prove permission rejection without execution",
                ):
                    runtime.validate_escalation("test")
                rejection_export["messages"][0]["parts"][0]["state"] = {
                    "status": "error",
                    "error": "The user rejected permission to use this specific tool call.",
                }

                log.write_text(
                    "\n".join(
                        valid_lines
                        + [
                            'evaluated permission=bash pattern="git push origin HEAD:main"'
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    runtime.RuntimeSmokeError,
                    "prohibited push reached Bash/permission evaluation",
                ):
                    runtime.validate_escalation("test")
            finally:
                runtime.workspace = original_workspace
                runtime.run_capture = original_run_capture

    def test_validate_leaf_contract_scopes_initialization_checks_to_leaf_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "issue"
            logs = base / "logs"
            logs.mkdir(parents=True)
            for task, slug, state, extra in (
                ("SMOKE-LEAF-COMPLETED", "depth2-leaf-completed", "completed", "parent accepted"),
                ("SMOKE-LEAF-BLOCKED", "depth2-leaf-blocked", "blocked", ""),
            ):
                path = base / "smoke-repo" / ".worktrees" / f"{task}-{slug}" / ".task-state" / "task.md"
                path.parent.mkdir(parents=True)
                path.write_text(f"Work Unit state={state}\n{extra}\n", encoding="utf-8")
                (path.parent / "work-units.json").write_text(
                    json.dumps({"units": {"WU-1": {"state": state}}}) + "\n",
                    encoding="utf-8",
                )
            (logs / "opencode-leaf-completed-20260101.log").write_text(
                "\n".join([
                    "message=created id=main agent=main",
                    "message=created id=to-completed parentID=main agent=task-orchestrator",
                    "initialization just agent::doctor just agent::context just project::doctor",
                    "message=created id=done parentID=to-completed title=completed agent=general",
                    'message="exiting loop" session.id=done',
                ]) + "\n", encoding="utf-8"
            )
            (logs / "opencode-leaf-blocked-20260101.log").write_text(
                "\n".join([
                    "message=created id=to-blocked parentID=main agent=task-orchestrator",
                    "message=created id=blocked parentID=to-blocked title=blocked agent=explore",
                    'message="exiting loop" session.id=blocked',
                ]) + "\n", encoding="utf-8"
            )
            original_workspace = runtime.workspace
            original_session_export = runtime._session_export
            runtime.workspace = lambda _issue: base
            evidence = {
                "to-completed": {
                    "session_id": "to-completed",
                    "assistant_final_status": None,
                    "tool_commands": ["just agent::doctor", "just agent::context", "just project::doctor"],
                    "tool_statuses": ["completed", "completed", "completed"],
                },
                "to-blocked": {
                    "session_id": "to-blocked",
                    "assistant_final_status": None,
                    "tool_commands": ["just agent::doctor", "just agent::context", "just project::doctor"],
                    "tool_statuses": ["completed", "completed", "completed"],
                },
                "done": {
                    "session_id": "done",
                    "assistant_final_status": "COMPLETED",
                    "tool_commands": ["git status --short"],
                    "tool_statuses": ["completed"],
                },
                "blocked": {
                    "session_id": "blocked",
                    "assistant_final_status": "BLOCKED",
                    "tool_commands": [],
                    "tool_statuses": [],
                },
            }
            runtime._session_export = lambda _repo, session_id, **_kwargs: evidence[session_id]
            try:
                result = runtime.validate_leaf_contract("test")
            finally:
                runtime.workspace = original_workspace
                runtime._session_export = original_session_export
            self.assertEqual("PASS", result["status"])
            self.assertEqual(
                {"completed": "COMPLETED", "blocked": "BLOCKED"},
                result["leafStatuses"],
            )

    def test_canonical_leaf_status_rejects_unknown_multiple_and_missing_fields(self) -> None:
        self.assertEqual(
            "BLOCKED",
            runtime._canonical_leaf_status("status: BLOCKED\n\nEvidence.", "leaf"),
        )
        for response in (
            "status: PASS",
            "Evidence before status.\nstatus: BLOCKED",
            "status: BLOCKED\nstatus: COMPLETED",
            "BLOCKED",
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(runtime.RuntimeSmokeError, "exactly one valid first-line status"):
                    runtime._canonical_leaf_status(response, "leaf")

    def test_sanitized_redacted_tool_input_cannot_satisfy_command_assertions(self) -> None:
        sanitized = {
            "messages": [{
                "info": {"role": "assistant"},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"redacted": "tool-input"},
                        },
                    },
                    {"type": "text", "text": "status: BLOCKED\n\nInitialization was denied."},
                ],
            }],
        }
        evidence = runtime._derive_session_evidence(
            sanitized, "leaf", require_final_status=True
        )
        self.assertEqual([None], evidence["tool_commands"])
        self.assertNotEqual(["git status --short"], evidence["tool_commands"])
        with self.assertRaisesRegex(runtime.RuntimeSmokeError, "did not complete initialization"):
            runtime._require_parent_initialization(evidence, "leaf")
        with self.assertRaisesRegex(runtime.RuntimeSmokeError, "missing or redacted"):
            runtime._require_leaf_tool_evidence(evidence, "completed", "leaf")

    def test_unsanitized_export_is_reduced_to_minimal_derived_evidence(self) -> None:
        unsanitized = {
            "messages": [
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": "full private prompt"}],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "bash",
                            "state": {
                                "status": "completed",
                                "input": {"command": "git status --short"},
                                "output": "full private tool output",
                            },
                        },
                        {
                            "type": "text",
                            "text": "status: COMPLETED\n\nfull private assistant explanation",
                        },
                    ],
                },
            ]
        }
        original_run = runtime.subprocess.run

        def fake_run(_command, **kwargs):
            json.dump(unsanitized, kwargs["stdout"])
            kwargs["stdout"].flush()
            return subprocess.CompletedProcess(_command, 0, stderr="")

        runtime.subprocess.run = fake_run
        try:
            evidence = runtime._session_export(
                Path("."), "leaf", require_final_status=True
            )
        finally:
            runtime.subprocess.run = original_run
        self.assertEqual(
            {
                "session_id": "leaf",
                "assistant_final_status": "COMPLETED",
                "tool_commands": ["git status --short"],
                "tool_statuses": ["completed"],
            },
            evidence,
        )
        rendered = json.dumps(evidence)
        self.assertNotIn("private", rendered)
        self.assertNotIn("output", rendered)
        self.assertNotIn("messages", rendered)

    def test_leaf_tool_evidence_rejects_blocked_bash_and_hides_command(self) -> None:
        evidence = {
            "session_id": "leaf",
            "assistant_final_status": "BLOCKED",
            "tool_commands": ["secret-command-value"],
            "tool_statuses": ["completed"],
        }
        with self.assertRaisesRegex(
            runtime.RuntimeSmokeError, "blocked leaf leaf attempted a Bash operation"
        ) as raised:
            runtime._require_leaf_tool_evidence(evidence, "blocked", "leaf")
        self.assertNotIn("secret-command-value", str(raised.exception))

    def test_trailing_empty_assistant_message_cannot_reuse_earlier_status(self) -> None:
        exported = {
            "messages": [
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "status: COMPLETED"}],
                },
                {"info": {"role": "assistant"}, "parts": []},
            ]
        }
        with self.assertRaisesRegex(
            runtime.RuntimeSmokeError, "exactly one valid first-line status"
        ):
            runtime._derive_session_evidence(
                exported, "leaf", require_final_status=True
            )

    def test_latest_complete_leaf_log_skips_newer_incomplete_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = root / "opencode-leaf-blocked-20260101.log"
            incomplete = root / "opencode-leaf-blocked-20260102.log"
            complete.write_text(
                "message=created id=parent parentID=main agent=task-orchestrator\n"
                "message=created id=leaf parentID=parent agent=explore\n"
                'message="exiting loop" session.id=leaf\n',
                encoding="utf-8",
            )
            incomplete.write_text(
                "message=created id=new-parent parentID=main agent=task-orchestrator\n",
                encoding="utf-8",
            )
            selected = runtime._latest_complete_leaf_log(
                [complete, incomplete], "leaf-blocked"
            )
        self.assertEqual((complete, "parent", "leaf"), selected)

    def test_transient_export_invalid_json_and_status_prefix_fail_closed(self) -> None:
        original_run = runtime.subprocess.run
        original_version = runtime._opencode_version
        runtime._opencode_version = lambda _repo: "1.18.test"

        def fake_run(_command, **kwargs):
            kwargs["stdout"].write('Exporting session...\n{"messages": []}')
            kwargs["stdout"].flush()
            return subprocess.CompletedProcess(_command, 0, stderr="bounded stderr")

        runtime.subprocess.run = fake_run
        try:
            with self.assertRaisesRegex(
                runtime.RuntimeSmokeError,
                r"opencode_version=1\.18\.test; exit_code=0; stderr=bounded stderr; "
                r"json_error=line 1 column 1 position 0",
            ) as raised:
                runtime._transient_session_export(Path("."), "leaf")
        finally:
            runtime.subprocess.run = original_run
            runtime._opencode_version = original_version
        self.assertNotIn("Exporting session", str(raised.exception))
        self.assertNotIn("messages", str(raised.exception))

    def test_parent_initialization_does_not_treat_unexecuted_checks_as_pass(self) -> None:
        evidence = {
            "session_id": "parent",
            "assistant_final_status": None,
            "tool_commands": ["just agent::doctor", "just agent::context"],
            "tool_statuses": ["completed", "pending"],
        }
        with self.assertRaisesRegex(runtime.RuntimeSmokeError, "did not complete initialization"):
            runtime._require_parent_initialization(evidence, "parent")

    def test_runtime_fixture_installs_explicit_native_ask_canary_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agents = repo / ".opencode" / "agents"
            agents.mkdir(parents=True)
            for name in runtime.RUNTIME_GENERAL_AGENTS:
                (agents / name).write_text(
                    "---\npermission:\n  task: deny\n  bash:\n"
                    '    "git status*": allow\n---\n',
                    encoding="utf-8",
                )

            runtime.harden_runtime_leaf_permissions(repo)
            runtime.harden_runtime_leaf_permissions(repo)

            profiles = []
            for name in runtime.RUNTIME_GENERAL_AGENTS:
                text = (agents / name).read_text(encoding="utf-8")
                self.assertEqual(1, text.count("  question: deny\n"), name)
                self.assertIn("  task: deny\n", text, name)
                self.assertIn("  bash:\n", text, name)
                self.assertEqual(1, text.count('    "*": deny\n'), name)
                self.assertIn('    "git status --short": allow\n', text, name)
                self.assertIn(
                    '    "printf \'depth2-ask-approved\\\\n\'": ask\n',
                    text,
                    name,
                )
                self.assertIn(
                    '    "printf \'depth2-ask-rejected\\\\n\'": ask\n',
                    text,
                    name,
                )
                self.assertNotIn(
                    '    "printf \'leaf-escalation-approved\\\\n\'": ask\n',
                    text,
                    name,
                )
                self.assertNotIn(
                    '    "git push origin HEAD:main": ask\n',
                    text,
                    name,
                )
                profiles.append(
                    tuple(
                        line
                        for line in text.splitlines()
                        if line
                        in {
                            '    "*": deny',
                            '    "git status --short": allow',
                            '    "printf \'depth2-ask-approved\\\\n\'": ask',
                            '    "printf \'depth2-ask-rejected\\\\n\'": ask',
                        }
                    )
                )
            self.assertEqual(1, len(profiles))

    def test_runtime_canary_extends_future_noninteractive_leaf_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agents = repo / ".opencode" / "agents"
            agents.mkdir(parents=True)
            (agents / "general.md").write_text(
                "---\npermission:\n  task: deny\n  question: deny\n  bash:\n"
                '    "*": deny\n'
                '    "just project::check": allow\n---\n',
                encoding="utf-8",
            )
            runtime.harden_runtime_leaf_permissions(repo)

            primary = (agents / "general.md").read_text(encoding="utf-8")
            self.assertEqual(1, primary.count('    "*": deny\n'))
            self.assertIn(
                '    "printf \'depth2-ask-approved\\\\n\'": ask\n',
                primary,
            )
            self.assertNotIn('    "printf \'leaf-escalation-approved\\\\n\'": ask\n', primary)
            self.assertNotIn('    "git push origin HEAD:main": ask\n', primary)
            self.assertIn('    "just project::check": allow\n', primary)

    def test_runtime_canary_rejects_interactive_leaf_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agents = repo / ".opencode" / "agents"
            agents.mkdir(parents=True)
            (agents / "general.md").write_text(
                "---\npermission:\n  task: deny\n  question: allow\n  bash:\n"
                '    "*": ask\n---\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runtime.RuntimeSmokeError,
                "non-deny question permission",
            ):
                runtime.harden_runtime_leaf_permissions(repo)

    def test_debug_launchers_use_official_diagnostic_paths_without_auto_approval(self) -> None:
        interactive = (ROOT / "tests" / "runtime" / "run_opencode_debug.sh").read_text(
            encoding="utf-8"
        )
        direct = (ROOT / "tests" / "runtime" / "run_direct_leaf.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--print-logs --log-level DEBUG", interactive)
        self.assertIn("snapshot-sessions", interactive)
        self.assertNotIn(" --auto", interactive)
        self.assertIn('"$OPENCODE_BIN" serve', direct)
        self.assertIn(
            'request("POST", "/session", {"title": "Direct leaf control"})', direct
        )
        self.assertIn('f"/session/{session_id}/message"', direct)
        self.assertIn('"agent": "general"', direct)
        self.assertNotIn('"model":', direct)
        self.assertIn("if len(tool_parts) != 1", direct)
        self.assertIn('["git", "status", "--porcelain"]', direct)
        self.assertIn("timeout --kill-after=20s", direct)
        self.assertNotIn(" --agent general", direct)
        self.assertNotIn(" --auto", direct)

    def test_debug_launcher_defaults_stderr_to_per_run_log_file(self) -> None:
        interactive = (ROOT / "tests" / "runtime" / "run_opencode_debug.sh").read_text(
            encoding="utf-8"
        )

        marker = 'if [[ "${OPENCODE_RUNTIME_LIVE_LOGS:-}" == "1" ]]; then'
        self.assertIn(marker, interactive)

        block_start = interactive.index(marker)
        block = interactive[
            block_start : interactive.index("\nfi", block_start)
        ]
        self.assertIn('2> >(tee "$log" >&2)', block)
        self.assertIn('2>"$log"', block)
        self.assertIn("else", block)
        self.assertGreater(block.index("else"), block.index('2> >(tee "$log" >&2)'))
        self.assertGreater(block.index('2>"$log"'), block.index("else"))

    def test_debug_launcher_opt_in_env_var_restores_live_stderr(self) -> None:
        interactive = (ROOT / "tests" / "runtime" / "run_opencode_debug.sh").read_text(
            encoding="utf-8"
        )

        marker = 'if [[ "${OPENCODE_RUNTIME_LIVE_LOGS:-}" == "1" ]]; then'
        self.assertIn(marker, interactive)
        self.assertIn("OPENCODE_RUNTIME_LIVE_LOGS", interactive)

        branch_block = interactive.split(marker, 1)[1]
        branch_block = branch_block.split("fi", 1)[0]
        self.assertIn("run_opencode_debug", branch_block)
        self.assertIn('2> >(tee "$log" >&2)', branch_block)
        self.assertIn("else", branch_block)
        self.assertIn('2>"$log"', branch_block)

    def test_debug_launcher_routes_stderr_and_preserves_exit_status(self) -> None:
        launcher = ROOT / "tests" / "runtime" / "run_opencode_debug.sh"
        with tempfile.TemporaryDirectory() as temporary:
            fake_root = Path(temporary)
            fake_bin = fake_root / "bin"
            fake_bin.mkdir()
            (fake_root / "tests" / "runtime").mkdir(parents=True)
            (fake_root / ".runtime-smoke" / "test" / "smoke-repo").mkdir(
                parents=True
            )

            def executable(name: str, body: str) -> None:
                path = fake_bin / name
                path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
                path.chmod(0o755)

            executable("git", 'printf "%s\\n" "$FAKE_ROOT"')
            executable("python3", "exit 0")
            executable("opencode", "exit 0")
            executable("nix", 'printf "debug-marker\\n" >&2\nexit 7')

            environment = os.environ.copy()
            environment["FAKE_ROOT"] = str(fake_root)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

            default = subprocess.run(
                ["bash", str(launcher), "test", "depth2-ask"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(7, default.returncode)
            self.assertEqual("", default.stderr)
            logs = list(
                (fake_root / ".runtime-smoke" / "test" / "logs").glob(
                    "opencode-depth2-ask-*.log"
                )
            )
            self.assertEqual(1, len(logs))
            self.assertEqual("debug-marker\n", logs[0].read_text(encoding="utf-8"))

            environment["OPENCODE_RUNTIME_LIVE_LOGS"] = "1"
            live = subprocess.run(
                ["bash", str(launcher), "test", "child-stall"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(7, live.returncode)
            self.assertEqual("debug-marker\n", live.stderr)
            live_logs = list(
                (fake_root / ".runtime-smoke" / "test" / "logs").glob(
                    "opencode-child-stall-*.log"
                )
            )
            self.assertEqual(1, len(live_logs))
            self.assertEqual(
                "debug-marker\n", live_logs[0].read_text(encoding="utf-8")
            )

    def test_prepare_commits_scaffold_before_nix_bootstrap_and_bootstraps_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "issue-41"
            calls: list[tuple[str, list[str], Path, Path]] = []

            original_workspace = runtime.workspace
            original_doctor = runtime.doctor
            original_run_logged = runtime.run_logged
            original_run_capture = runtime.run_capture
            original_write_contract = runtime.write_contract
            original_harden_permissions = runtime.harden_runtime_leaf_permissions
            hardened_paths: list[Path] = []

            def fake_workspace(_: str) -> Path:
                return base

            def fake_doctor() -> dict:
                return {"status": "PASS"}

            def fake_run_logged(command: list[str], *, cwd: Path, log: Path) -> None:
                calls.append(("logged", command, cwd, log))

            def fake_run_capture(command: list[str], *, cwd: Path) -> str:
                if command == ["opencode", "--version"]:
                    return "1.18.4"
                if command == ["git", "rev-parse", "HEAD"]:
                    return "cafebabe"
                if command == ["git", "status", "--porcelain"]:
                    return ""
                return "result"

            runtime.workspace = fake_workspace
            runtime.doctor = fake_doctor
            runtime.run_logged = fake_run_logged
            runtime.run_capture = fake_run_capture
            runtime.write_contract = lambda *args, **kwargs: None
            runtime.harden_runtime_leaf_permissions = lambda path: hardened_paths.append(path)
            try:
                runtime.prepare("issue-41", "agent-python")
            finally:
                runtime.workspace = original_workspace
                runtime.doctor = original_doctor
                runtime.run_logged = original_run_logged
                runtime.run_capture = original_run_capture
                runtime.write_contract = original_write_contract
                runtime.harden_runtime_leaf_permissions = original_harden_permissions

            commands = [command for _kind, command, *_ in calls]

            def index_of(prefix: list[str]) -> int:
                for idx, command in enumerate(commands):
                    if command[: len(prefix)] == prefix:
                        return idx
                raise AssertionError(f"command {prefix} not found")

            flake = index_of(["nix", "flake", "init"])
            git_init = index_of(["git", "init", "-b", "main"])
            bootstrap = index_of(["nix", "develop", "--command", "just", "project::bootstrap", "smoke-project"])
            first_commit = index_of(["git", "commit", "-m", "Initialize runtime smoke fixture"])
            second_commit = index_of(
                [
                    "nix",
                    "develop",
                    "--command",
                    "git",
                    "commit",
                    "-m",
                    "Bootstrap runtime smoke fixture",
                ]
            )
            add_indices = [
                idx for idx, command in enumerate(commands) if command == ["git", "add", "."]
            ]

            self.assertLess(git_init, bootstrap)
            self.assertLess(first_commit, bootstrap)
            self.assertLess(bootstrap, second_commit)
            self.assertLess(flake, first_commit)
            self.assertLess(flake, bootstrap)
            self.assertEqual(2, len(add_indices))
            self.assertLess(add_indices[0], first_commit)
            self.assertGreater(second_commit, bootstrap)
            self.assertGreater(add_indices[1], bootstrap)
            self.assertIn(
                ["nix", "develop", "--command", "git", "push", "-u", "origin", "main"],
                commands,
            )
            ask_worktree = base / "smoke-repo" / ".worktrees" / "SMOKE-ASK-depth2-ask"
            self.assertEqual(hardened_paths, [ask_worktree])
            self.assertIn(
                [
                    "git",
                    "add",
                    ".opencode/agents/general.md",
                ],
                commands,
            )
            self.assertIn(
                [
                    "nix",
                    "develop",
                    "--command",
                    "git",
                    "commit",
                    "-m",
                    "Install native Ask canary profile",
                ],
                commands,
            )

    def test_report_template_requires_evidence_based_classification(self) -> None:
        metadata = {
            "templatesCommit": "abc",
            "opencodeVersion": "1.18.4",
            "template": "agent-python",
            "smokeRepo": "/tmp/smoke",
            "createdAt": "2026-08-09T00:00:00+00:00",
        }
        report = runtime.report_template("issue-41", metadata)
        self.assertIn("Ask-free nested control", report)
        self.assertIn("Direct leaf control", report)
        self.assertIn("Depth-2 leaf Work Unit contracts", report)
        self.assertIn("Depth-2 native Ask compatibility canary", report)
        self.assertIn("Leaf → Depth-1 escalation release gate", report)
        self.assertIn("release blocker: NO", report)
        self.assertIn("#51 release gate (Leaf -> Depth-1 escalation)", report)
        self.assertIn("#7 release status", report)
        self.assertIn("SMOKE-ESCALATION contract: READY", report)
        self.assertIn("SMOKE-LEAF-COMPLETED contract: READY", report)
        self.assertIn("SMOKE-LEAF-BLOCKED contract: READY", report)
        self.assertIn("SMOKE-ESCALATION-PERMISSION contract: READY", report)
        self.assertIn("Ask origin is Task Orchestrator session", report)
        self.assertIn("`runtime::validate-escalation`", report)
        self.assertIn("must remain INCOMPLETE", report)
        self.assertIn("configured fixed model", report)
        self.assertIn("no model substitution", report)
        self.assertIn("PASS / FAIL / INCOMPLETE", report)
        self.assertIn("Do not infer PASS", report)


if __name__ == "__main__":
    unittest.main()
