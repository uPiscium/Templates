from __future__ import annotations

import importlib.util
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
            "smoke-fallback issue='issue-41'",
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
                ("SMOKE-CONTROL", "ask-free-control"),
                ("SMOKE-ASK", "depth2-ask"),
                ("SMOKE-FALLBACK", "model-fallback"),
            },
        )

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

    def test_runtime_fixture_installs_explicit_native_ask_canary_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agent = repo / ".opencode" / "agents" / "general.md"
            agent.parent.mkdir(parents=True)
            agent.write_text(
                "---\npermission:\n  task: deny\n  bash:\n"
                '    "git status*": allow\n---\n',
                encoding="utf-8",
            )

            runtime.harden_runtime_leaf_permissions(repo)
            runtime.harden_runtime_leaf_permissions(repo)

            text = agent.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("  question: deny\n"))
            self.assertIn("  task: deny\n", text)
            self.assertIn("  bash:\n", text)
            self.assertEqual(1, text.count('    "*": deny\n'))
            self.assertIn('    "git status --short": allow\n', text)
            self.assertIn(
                '    "printf \'depth2-ask-approved\\\\n\'": ask\n', text
            )
            self.assertIn(
                '    "printf \'depth2-ask-rejected\\\\n\'": ask\n', text
            )

    def test_runtime_canary_extends_future_noninteractive_leaf_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agent = repo / ".opencode" / "agents" / "general.md"
            agent.parent.mkdir(parents=True)
            agent.write_text(
                "---\npermission:\n  task: deny\n  question: deny\n  bash:\n"
                '    "*": deny\n'
                '    "just project::check": allow\n---\n',
                encoding="utf-8",
            )

            runtime.harden_runtime_leaf_permissions(repo)

            text = agent.read_text(encoding="utf-8")
            self.assertEqual(1, text.count('    "*": deny\n'))
            self.assertIn('    "just project::check": allow\n', text)
            self.assertIn(
                '    "printf \'depth2-ask-approved\\\\n\'": ask\n', text
            )

    def test_runtime_canary_rejects_interactive_leaf_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            agent = repo / ".opencode" / "agents" / "general.md"
            agent.parent.mkdir(parents=True)
            agent.write_text(
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
            runtime.harden_runtime_leaf_permissions = lambda *args, **kwargs: None
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
        self.assertIn("Depth-2 native Ask compatibility canary", report)
        self.assertIn("release blocker: NO", report)
        self.assertIn("#7 release gate: Leaf -> Depth-1 escalation", report)
        self.assertIn("PASS / FAIL / INCOMPLETE", report)
        self.assertIn("Do not infer PASS", report)


if __name__ == "__main__":
    unittest.main()
