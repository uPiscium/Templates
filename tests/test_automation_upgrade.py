from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
SPEC = importlib.util.spec_from_file_location("automation_upgrade", SCRIPT)
assert SPEC and SPEC.loader
upgrade = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upgrade
SPEC.loader.exec_module(upgrade)

AGENT_SPEC = importlib.util.spec_from_file_location(
    "agent_core_for_upgrade_tests", ROOT / "components" / "agent-core" / ".automation" / "bin" / "agent_core.py"
)
assert AGENT_SPEC and AGENT_SPEC.loader
agent_core = importlib.util.module_from_spec(AGENT_SPEC)
AGENT_SPEC.loader.exec_module(agent_core)


class AutomationUpgradeContractTest(unittest.TestCase):
    TEMPLATE_NAMES = ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake")
    V3_REMOVED_PATHS = (
        ".automation/model-fallback.toml",
        ".automation/bin/model_fallback.py",
        ".opencode/commands/task-recover.md",
        ".opencode/commands/task-recover-clear.md",
        ".opencode/skills/task-recovery/SKILL.md",
        ".opencode/agents/architect-fallback.md",
        ".opencode/agents/build-fallback.md",
        ".opencode/agents/explore-fallback.md",
        ".opencode/agents/general-fallback.md",
        ".opencode/agents/investigator-fallback.md",
        ".opencode/agents/plan-fallback.md",
        ".opencode/agents/reviewer-fallback.md",
        ".opencode/agents/scout-fallback.md",
        ".opencode/agents/security-reviewer-fallback.md",
        ".opencode/agents/task-orchestrator-fallback.md",
        ".opencode/agents/verifier-fallback.md",
    )

    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _field(self, value, name: str):
        if isinstance(value, dict):
            return value[name]
        return getattr(value, name)

    def _require_migration_api(self) -> None:
        for fn in ("load_migrations", "build_plan", "apply"):
            if not hasattr(upgrade, fn):
                self.skipTest(f"{fn} API is not present in this checkout")

    def _core_root_source(self, source_root: Path, *, version: int = 3, migrations: str | None = None) -> Path:
        source = source_root / "components" / "agent-core"
        auto = source / ".automation"
        auto.mkdir(parents=True)

        migration_body = migrations if migrations is not None else "schema_version = 1\n"
        self._write_file(auto / "migrations.toml", migration_body)
        self._write_file(auto / "VERSION", f"{version}\n")

        self._write_file(
            source / "AGENTS.md",
            """<!-- BEGIN AGENT CORE RULES -->
core rules
<!-- END AGENT CORE RULES -->
""",
        )
        self._write_file(
            source / "Justfile",
            """# Agent Core module router
mod agent '.automation/just/agent.just'
mod project 'just/project/mod.just'
""",
        )
        self._write_file(source / "opencode.json", "{}\n")
        self._write_file(auto / "UPSTREAM", 'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n')
        return source

    def _destination_repo(self, repo_root: Path, *, version: int = 2) -> Path:
        self._write_file(repo_root / ".automation" / "VERSION", f"{version}\n")
        self._write_file(
            repo_root / ".automation" / "ADAPTER",
            "base\n",
        )
        self._write_file(
            repo_root / ".automation" / "UPSTREAM",
            'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n',
        )
        self._write_file(
            repo_root / ".automation" / "ownership.toml",
            'version = 1\n\n[paths]\n"AGENTS.md" = "replace"\n"Justfile" = "replace"\n',
        )

        self._write_file(
            repo_root / "AGENTS.md",
            """<!-- BEGIN AGENT CORE RULES -->
existing core rules
<!-- END AGENT CORE RULES -->
""",
        )
        self._write_file(
            repo_root / "Justfile",
            """# Agent Core module router
mod agent '.automation/just/agent.just'
mod project 'just/project/mod.just'
""",
        )
        self._write_file(repo_root / "opencode.json", "{}\n")
        return repo_root

    def _plan_action(self, plan: dict, path: str):
        for item in plan["actions"]:
            if item["path"] == path:
                return item
        return None

    def _manifest(self, transitions: list[str], *, extra_top: str = "") -> str:
        body = ["schema_version = 1", ""]
        if extra_top:
            body.append(extra_top)
            body.append("")
        body.extend(transitions)
        return "\n".join(body)

    def _migration_manifest(
        self,
        from_version: int = 2,
        to_version: int = 3,
        *,
        remove_paths: tuple[str, ...] = (),
        require_absent_paths: tuple[str, ...] = (),
    ) -> str:
        def render(values: tuple[str, ...]) -> str:
            return "[" + ", ".join(json.dumps(value) for value in values) + "]"

        return (
            "schema_version = 1\n\n"
            "[[migrations]]\n"
            f"from_version = {from_version}\n"
            f"to_version = {to_version}\n"
            f"remove_paths = {render(remove_paths)}\n"
            f"require_absent_paths = {render(require_absent_paths)}\n"
        )

    def _git(self, args: list[str], cwd: Path) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def _task_repo(self, repo: Path, *, task: str = "TASK-78") -> Path:
        repo.mkdir(parents=True)
        self._git(["init", "-b", "main"], repo)
        self._git(["config", "user.name", "Test User"], repo)
        self._git(["config", "user.email", "test@example.invalid"], repo)
        self._write_file(repo / "README.md", "repository\n")
        self._git(["add", "README.md"], repo)
        self._git(["commit", "-m", "initial"], repo)
        self._git(["switch", "-c", f"task/{task}-maintenance"], repo)
        head = self._git(["rev-parse", "HEAD"], repo)
        self._git(["update-ref", "refs/remotes/origin/main", head], repo)
        self._git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], repo)
        state = f"- Task ID: {task}\n- Branch: task/{task}-maintenance\n- Worktree: {repo.resolve()}\n"
        self._write_file(repo / ".task-state/task.md", state)
        exclude = repo / ".git/info/exclude"
        exclude.write_text("/.task-state/\n", encoding="utf-8")
        return repo

    def _apply_fixture(self, root: Path, *, source_git: bool = True) -> tuple[Path, Path]:
        repo = self._task_repo(root / "repo")
        self._destination_repo(repo, version=2)
        self._git(["add", "-A"], repo)
        self._git(["commit", "-m", "fixture"], repo)
        source = self._core_root_source(root, version=3)
        if source_git:
            self._git(["init", "-b", "main"], root)
            self._git(["config", "user.name", "Test User"], root)
            self._git(["config", "user.email", "test@example.invalid"], root)
            self._git(["add", "components"], root)
            self._git(["commit", "-m", "source"], root)
        with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
            upgrade.apply(repo, root)
        return repo, source

    def _receipt(self, repo: Path) -> dict:
        return json.loads(upgrade.receipt_path(repo).read_text(encoding="utf-8"))

    def _commit_error(self, repo: Path, task: str = "TASK-78") -> str:
        with self.assertRaises(upgrade.UpgradeError) as raised:
            upgrade.commit(repo, task, "maintenance")
        return str(raised.exception)

    def _mock_maintenance(self, repo: Path) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(
                upgrade,
                "require_maintenance",
                return_value=("TASK-TEST", "task/TASK-TEST-test", repo),
            )
        )
        stack.enter_context(mock.patch.object(upgrade, "write_authority"))
        return stack

    def test_upstream_and_generated_parity(self) -> None:
        upstream = ROOT / "components" / "agent-core" / ".automation" / "UPSTREAM"
        ownership = ROOT / "components" / "agent-core" / ".automation" / "ownership.toml"
        self.assertIn('repository = "github:upiscium/Templates"', upstream.read_text())
        self.assertIn('"AGENTS.md" = "replace"', ownership.read_text())
        for template in ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertEqual(
                upstream.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "UPSTREAM").read_bytes(),
            )
            self.assertEqual(
                ownership.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "ownership.toml").read_bytes(),
            )
            self.assertEqual(
                SCRIPT.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "bin" / "automation_upgrade.py").read_bytes(),
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
        script = SCRIPT.read_text()
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
        self.assertIn("operation refused on the default branch", script)
        self.assertIn("commitCreated", script)
        self.assertIn("pushPerformed", script)
        self.assertIn("mergePerformed", script)

    def test_existing_repository_plan_merges_managed_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            automation = repo / ".automation"
            automation.mkdir()
            (automation / "VERSION").write_text("1\n")
            (automation / "ADAPTER").write_text("base\n")
            (automation / "UPSTREAM").write_text(
                'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n'
            )
            (automation / "ownership.toml").write_text(
                'version = 1\n\n[paths]\n"AGENTS.md" = "replace"\n"Justfile" = "replace"\n'
            )
            core_rules = (ROOT / "components" / "agent-core" / "AGENTS.md").read_text()
            (repo / "AGENTS.md").write_text(
                "# Existing Repository Rules\n\n"
                "<!-- BEGIN AGENT CORE RULES -->\nold core rules\n<!-- END AGENT CORE RULES -->\n"
            )
            (repo / "Justfile").write_text(
                "default:\n    @echo existing\n\n"
                "# Agent Core module router\n"
                "mod agent '.automation/just/agent.just'\n"
                "mod integrate '.automation/just/integrate.just'\n"
                "mod project 'just/project/mod.just'\n"
                "mod? local 'just/local.just'\n"
            )

            plan = upgrade.build_plan(repo, ROOT)
            actions = {item["path"]: item for item in plan["actions"]}
            self.assertEqual(actions["AGENTS.md"]["action"], "merge")
            self.assertEqual(actions["Justfile"]["action"], "merge")
            self.assertTrue(plan["canApply"], plan["blockers"])

            merged_agents, _ = upgrade.replace_agent_rules(
                (repo / "AGENTS.md").read_text(), core_rules
            )
            assert merged_agents is not None
            self.assertIn("# Existing Repository Rules", merged_agents)
            self.assertIn(core_rules.rstrip(), merged_agents)

            merged_just, _ = upgrade.merge_just_router(
                (repo / "Justfile").read_text(),
                (ROOT / "components" / "agent-core" / "Justfile").read_text(),
            )
            assert merged_just is not None
            self.assertIn("@echo existing", merged_just)
            self.assertIn("mod automation '.automation/just/automation.just'", merged_just)

    def test_malformed_rules_and_conflicting_router_block_upgrade(self) -> None:
        bad_agents, reason = upgrade.replace_agent_rules(
            "<!-- BEGIN AGENT CORE RULES -->\nmissing end\n", "new rules\n"
        )
        self.assertIsNone(bad_agents)
        self.assertIn("malformed", reason)

        bad_just, reason = upgrade.merge_just_router(
            "mod agent 'custom/agent.just'\n",
            (ROOT / "components" / "agent-core" / "Justfile").read_text(),
        )
        self.assertIsNone(bad_just)
        self.assertIn("repository-owned path", reason)

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

    def test_load_migrations_accepts_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations="schema_version = 1\n")
            self.assertEqual([], upgrade.load_migrations(source))

    def test_load_migrations_accepts_valid_transition(self) -> None:
        manifest = self._migration_manifest(
            remove_paths=(".automation/obsolete.py",),
            require_absent_paths=(".task-state/recovery.json",),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), version=3, migrations=manifest)
            migrations = upgrade.load_migrations(source)
            self.assertEqual(1, len(migrations))
            self.assertEqual((2, 3), (migrations[0].from_version, migrations[0].to_version))

    def test_load_migrations_rejects_unknown_top_level_key(self) -> None:
        manifest = "schema_version = 1\nlegacy = true\n"
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "unknown top-level"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_unknown_migration_field(self) -> None:
        manifest = self._migration_manifest() + "legacy = true\n"
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "unknown fields"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_non_integer_versions(self) -> None:
        manifest = self._migration_manifest().replace("from_version = 2", 'from_version = "2"')
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "positive integers"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_non_consecutive_versions(self) -> None:
        manifest = self._migration_manifest(1, 3)
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "exactly one version"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_duplicate_transition(self) -> None:
        transition = self._migration_manifest().split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + transition + "\n" + transition
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "duplicates transition"):
                upgrade.load_migrations(source)

    def test_remove_path_safety_rejects_absolute_traversal_and_glob(self) -> None:
        for path in ("/tmp/escape", "../escape", ".opencode/*.md"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                source = self._core_root_source(
                    Path(directory), migrations=self._migration_manifest(remove_paths=(path,))
                )
                with self.assertRaisesRegex(upgrade.UpgradeError, "unsafe|exact repository-relative"):
                    upgrade.load_migrations(source)

    def test_remove_path_safety_rejects_unmanaged_and_protected_paths(self) -> None:
        for path in (".automation/ADAPTER", "just/project/mod.just", "README.md", "AGENTS.md"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                source = self._core_root_source(
                    Path(directory), migrations=self._migration_manifest(remove_paths=(path,))
                )
                with self.assertRaisesRegex(upgrade.UpgradeError, "not Agent Core managed"):
                    upgrade.load_migrations(source)

    def test_build_plan_adds_delete_action_for_v2_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".automation/obsolete.py", "obsolete\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete.py",))
            )
            action = self._plan_action(upgrade.build_plan(repo, root), ".automation/obsolete.py")
            self.assertEqual("delete", action["action"])
            self.assertEqual("removed by Agent Core migration 2 -> 3", action["reason"])

    def test_build_plan_absent_obsolete_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete.py",))
            )
            plan = upgrade.build_plan(repo, root)
            self.assertEqual("noop", self._plan_action(plan, ".automation/obsolete.py")["action"])

    def test_build_plan_require_absent_present_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".task-state/recovery.json", "{}\n")
            self._core_root_source(
                root,
                version=3,
                migrations=self._migration_manifest(require_absent_paths=(".task-state/recovery.json",)),
            )
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            message = "\n".join(plan["blockers"])
            self.assertIn("migration 2 -> 3", message)
            self.assertIn(".task-state/recovery.json", message)
            self.assertIn("operator must resolve", message)

    def test_build_plan_require_absent_absent_can_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._core_root_source(
                root,
                version=3,
                migrations=self._migration_manifest(require_absent_paths=(".task-state/recovery.json",)),
            )
            self.assertTrue(upgrade.build_plan(repo, root)["canApply"])

    def test_build_plan_v1_to_v3_selects_later_migrations(self) -> None:
        first = self._migration_manifest(1, 2, remove_paths=(".automation/one",)).split("\n\n", 1)[1]
        second = self._migration_manifest(2, 3, remove_paths=(".automation/two",)).split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + first + "\n" + second
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=1)
            self._write_file(repo / ".automation/one", "1\n")
            self._write_file(repo / ".automation/two", "2\n")
            self._core_root_source(root, version=3, migrations=manifest)
            plan = upgrade.build_plan(repo, root)
            self.assertEqual("delete", self._plan_action(plan, ".automation/one")["action"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/two")["action"])

    def test_later_precondition_accepts_path_planned_for_earlier_deletion(self) -> None:
        first = self._migration_manifest(1, 2, remove_paths=(".automation/old",)).split("\n\n", 1)[1]
        second = self._migration_manifest(
            2,
            3,
            require_absent_paths=(".automation/old",),
        ).split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + first + "\n" + second
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=1)
            self._write_file(repo / ".automation/old", "old\n")
            self._core_root_source(root, version=3, migrations=manifest)
            plan = upgrade.build_plan(repo, root)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/old")["action"])

    def test_build_plan_rejects_downgrade(self) -> None:
        self._require_migration_api()
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=4)
            source = self._core_root_source(tmp, version=3)
            plan = upgrade.build_plan(repo, tmp)
            self.assertFalse(plan["canApply"])
            self.assertIn("downgrade", "\n".join(plan["blockers"]).lower())

    def test_apply_deletes_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".automation/stale.py", "to delete\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/stale.py",))
            )
            with self._mock_maintenance(repo):
                result = upgrade.apply(repo, root)
            self.assertIn(".automation/stale.py", result["changedPaths"])
            self.assertFalse((repo / ".automation/stale.py").exists())

    def test_migration_catches_up_residue_after_version_already_advanced(self) -> None:
        manifest = self._migration_manifest(remove_paths=(".automation/legacy.py",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=3)
            self._write_file(repo / ".automation/legacy.py", "legacy\n")
            self._write_file(repo / "opencode.json", "before\n")
            source = self._core_root_source(root, version=3, migrations=manifest)
            self._write_file(source / "opencode.json", "after\n")

            plan = upgrade.build_plan(repo, root)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/legacy.py")["action"])

            with self._mock_maintenance(repo):
                upgrade.apply(repo, root)
            self.assertFalse((repo / ".automation/legacy.py").exists())
            self.assertEqual("3\n", (repo / ".automation/VERSION").read_text())
            self.assertEqual("after\n", (repo / "opencode.json").read_text())

    def test_apply_symlink_removed_without_deleting_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = repo / "target.txt"
            self._write_file(target, "target\n")
            os.symlink(target, repo / ".automation/link.py")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/link.py",))
            )
            with self._mock_maintenance(repo):
                upgrade.apply(repo, root)
            self.assertFalse((repo / ".automation/link.py").exists())
            self.assertTrue(target.exists())

    def test_directory_delete_collision_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            (repo / ".automation/obsolete").mkdir()
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete",))
            )
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("refuses directory deletion", "\n".join(plan["blockers"]))

    def test_managed_destination_symlink_blocks_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = root / "external.json"
            self._write_file(target, "external\n")
            (repo / "opencode.json").unlink()
            os.symlink(target, repo / "opencode.json")
            self._core_root_source(root, version=2)
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("destination symlink", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root)
            self.assertEqual("external\n", target.read_text())

    def test_non_directory_managed_parent_blocks_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".opencode", "collision\n")
            source = self._core_root_source(root, version=3)
            self._write_file(source / ".opencode/agents/new.md", "new\n")
            self._write_file(repo / "opencode.json", "before\n")
            self._write_file(source / "opencode.json", "after\n")
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("non-directory ancestor .opencode", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root)
            self.assertEqual("2\n", (repo / ".automation/VERSION").read_text())
            self.assertEqual("before\n", (repo / "opencode.json").read_text())

    def test_managed_symlink_ancestor_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = root / "external-agents"
            target.mkdir()
            (repo / ".opencode").symlink_to(target, target_is_directory=True)
            source = self._core_root_source(root, version=2)
            self._write_file(source / ".opencode/agents/new.md", "new\n")
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("symlink ancestor .opencode", "\n".join(plan["blockers"]))
            self.assertEqual([], list(target.iterdir()))

    def test_apply_blocked_path_prevents_all_mutations_and_version_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            (repo / ".automation/obsolete").mkdir()
            self._write_file(repo / "opencode.json", "{\"before\":1}\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete",))
            )
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root)
            self.assertEqual((repo / ".automation" / "VERSION").read_text(), "2\n")
            self.assertEqual((repo / "opencode.json").read_text(), "{\"before\":1}\n")

    def test_apply_writes_VERSION_last_after_source_actions(self) -> None:
        manifest = self._migration_manifest(remove_paths=(".automation/stale.py",))
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=2)
            self._write_file(repo / ".automation/stale.py", "stale\n")
            self._write_file(repo / "opencode.json", "{\"before\":1}\n")
            source = self._core_root_source(tmp, version=3, migrations=manifest)
            self._write_file(source / "opencode.json", "{\"before\":2}\n")

            calls: list[str] = []
            original_copy2 = upgrade.shutil.copy2

            def _mock_copy2(src: Path, dst: Path, *args, **kwargs):
                if Path(dst) != repo / ".automation/VERSION":
                    self.assertEqual("2\n", (repo / ".automation/VERSION").read_text())
                calls.append(str(Path(dst)))
                return original_copy2(src, dst, *args, **kwargs)

            with self._mock_maintenance(repo):
                with mock.patch.object(upgrade.shutil, "copy2", side_effect=_mock_copy2):
                    upgrade.apply(repo, tmp)
            self.assertTrue(calls, calls)
            self.assertEqual(calls[-1], str(repo / ".automation" / "VERSION"))
            self.assertFalse((repo / ".automation/stale.py").exists())
            self.assertEqual((repo / ".automation" / "VERSION").read_text(), "3\n")

    def test_adapter_is_preserved_by_plan_and_apply(self) -> None:
        manifest = "schema_version = 1\n"
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=2)
            source = self._core_root_source(tmp, version=2, migrations=manifest)
            source_adp = source / ".automation" / "ADAPTER"
            source_parent = source_adp.parent
            source_parent.mkdir(parents=True, exist_ok=True)
            self._write_file(source_adp, "python\n")
            plan = upgrade.build_plan(repo, tmp)
            self.assertIsNone(self._plan_action(plan, ".automation/ADAPTER"))
            with self._mock_maintenance(repo):
                with mock.patch.object(upgrade.shutil, "copy2", side_effect=shutil.copy2):
                    upgrade.apply(repo, tmp)
            self.assertEqual((repo / ".automation" / "ADAPTER").read_text(), "base\n")

    def test_current_v2_to_v3_migration_removes_obsolete_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            (repo / ".automation" / "VERSION").write_text("2\n", encoding="utf-8")
            for path in self.V3_REMOVED_PATHS:
                self._write_file(repo / path, "obsolete\n")
            plan = upgrade.build_plan(repo, ROOT)
            self.assertTrue(plan["canApply"], plan["blockers"])
            for path in self.V3_REMOVED_PATHS:
                self.assertEqual("delete", self._plan_action(plan, path)["action"], path)

            migrations = upgrade.load_migrations(ROOT / "components/agent-core")
            self.assertEqual(1, len(migrations))
            migration = migrations[0]
            self.assertEqual((2, 3), (migration.from_version, migration.to_version))
            self.assertEqual(tuple(map(Path, self.V3_REMOVED_PATHS)), migration.remove_paths)
            self.assertEqual((Path(".task-state/recovery.json"),), migration.require_absent_paths)

            with self._mock_maintenance(repo):
                upgrade.apply(repo, ROOT)
            for path in self.V3_REMOVED_PATHS:
                self.assertFalse((repo / path).exists(), path)
            self.assertEqual("3\n", (repo / ".automation" / "VERSION").read_text())

    def test_current_v2_to_v3_active_recovery_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            (repo / ".automation" / "VERSION").write_text("2\n", encoding="utf-8")
            obsolete = repo / self.V3_REMOVED_PATHS[0]
            self._write_file(obsolete, "obsolete\n")
            recovery = repo / ".task-state" / "recovery.json"
            self._write_file(recovery, "{}\n")

            plan = upgrade.build_plan(repo, ROOT)
            self.assertFalse(plan["canApply"])
            self.assertIn(".task-state/recovery.json", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, ROOT)
            self.assertTrue(obsolete.is_file())
            self.assertTrue(recovery.is_file())
            self.assertEqual("2\n", (repo / ".automation" / "VERSION").read_text())

    def test_current_v3_catches_up_obsolete_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            obsolete = repo / self.V3_REMOVED_PATHS[-1]
            self._write_file(obsolete, "obsolete\n")
            plan = upgrade.build_plan(repo, ROOT)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, self.V3_REMOVED_PATHS[-1])["action"])
            with self._mock_maintenance(repo):
                upgrade.apply(repo, ROOT)
            self.assertFalse(obsolete.exists())
            self.assertEqual("3\n", (repo / ".automation" / "VERSION").read_text())

    def test_template_migrations_and_upgrade_script_parity_after_render(self) -> None:
        for template in self.TEMPLATE_NAMES:
            template_core = ROOT / "templates" / template
            self.assertTrue((template_core / ".automation" / "migrations.toml").exists())
            self.assertEqual(
                (ROOT / "components" / "agent-core" / ".automation" / "migrations.toml").read_bytes(),
                (template_core / ".automation" / "migrations.toml").read_bytes(),
            )
            self.assertEqual(
                SCRIPT.read_bytes(),
                (template_core / ".automation" / "bin" / "automation_upgrade.py").read_bytes(),
            )

    def test_generated_automation_script_just_and_opencode_parity(self) -> None:
        generated = (
            ".automation/bin/automation_upgrade.py",
            ".automation/just/automation.just",
            "opencode.json",
        )
        for template in self.TEMPLATE_NAMES:
            with self.subTest(template=template):
                template_root = ROOT / "templates" / template
                for relative in generated:
                    self.assertEqual(
                        (ROOT / "components" / "agent-core" / relative).read_bytes(),
                        (template_root / relative).read_bytes(),
                        relative,
                    )

    def test_apply_writes_complete_receipt_and_replaces_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source = self._apply_fixture(Path(directory))
            receipt = self._receipt(repo)
            self.assertEqual(1, receipt["schema_version"])
            self.assertEqual("TASK-78", receipt["task_id"])
            self.assertEqual("task/TASK-78-maintenance", receipt["branch"])
            self.assertEqual(str(repo.resolve()), receipt["worktree"])
            self.assertEqual(str(Path(directory).resolve()), receipt["source"])
            self.assertEqual(upgrade.source_revision(Path(directory)), receipt["source_revision"])
            self.assertEqual(["2", "3"], [receipt["current_version"], receipt["upstream_version"]])
            self.assertEqual(sorted(receipt["changed_paths"]), receipt["changed_paths"])
            self.assertTrue(receipt["path_fingerprints"])
            agents = (repo / "AGENTS.md").read_text()
            self.assertIn("BEGIN AGENT CORE RULES", agents)
            self.assertIn("core rules", agents)
            self.assertIn("END AGENT CORE RULES", agents)
            self.assertIn("mod agent", (repo / "Justfile").read_text())
            self.assertEqual("{}\n", (repo / "opencode.json").read_text())
            self.assertEqual("base\n", (repo / ".automation/ADAPTER").read_text())
            self.assertEqual(source, Path(receipt["source"]) / "components" / "agent-core")

    def test_valid_receipt_commit_uses_real_git_repo_and_consumes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            result = upgrade.commit(repo, "TASK-78", "automation maintenance")
            self.assertEqual("COMMITTED", result["status"])
            self.assertEqual(result["commit_sha"], upgrade.git_head(repo))
            self.assertFalse(upgrade.receipt_path(repo).exists())
            consumed = json.loads(upgrade.consumed_receipt_path(repo).read_text())
            self.assertEqual("consumed", consumed["status"])
            self.assertEqual(result["commit_sha"], consumed["commit_sha"])
            self.assertEqual([], upgrade.pending_paths(repo))

    def test_second_successful_upgrade_replaces_consumed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root / "first")
            first = self._receipt(repo)
            upgrade.commit(repo, "TASK-78", "first maintenance")
            consumed = upgrade.consumed_receipt_path(repo)
            self.assertTrue(consumed.exists())

            second_root = root / "second"
            second_source = self._core_root_source(second_root, version=4)
            (second_source / "AGENTS.md").write_bytes((repo / "AGENTS.md").read_bytes())
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                upgrade.apply(repo, second_root)
            second = self._receipt(repo)
            self.assertTrue(upgrade.receipt_path(repo).exists())
            self.assertFalse(consumed.exists())
            self.assertNotEqual(first["authority_head"], second["authority_head"])
            self.assertEqual("3", second["current_version"])
            self.assertEqual("4", second["upstream_version"])
            self.assertEqual([".automation/VERSION"], second["changed_paths"])
            self.assertNotIn("README.md", second["changed_paths"])

    def test_no_change_upgrade_preserves_consumed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            upgrade.commit(repo, "TASK-78", "maintenance")
            consumed_before = upgrade.consumed_receipt_path(repo).read_bytes()

            no_changes = {
                "currentVersion": "3",
                "upstreamVersion": "3",
                "actions": [],
                "blockers": [],
            }
            with mock.patch.dict(
                os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False
            ), mock.patch.object(upgrade, "build_plan", return_value=no_changes):
                result = upgrade.apply(repo, root)

            self.assertEqual("NO_CHANGES", result["status"])
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertEqual(consumed_before, upgrade.consumed_receipt_path(repo).read_bytes())

    def test_forged_receipt_without_upgrade_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            upgrade.authority_path(repo).unlink()
            self.assertIn("successful-upgrade authority", self._commit_error(repo))

    def test_git_hooks_cannot_expand_maintenance_commit_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            hooks = Path(directory) / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf 'hooked\\n' > product-from-hook.txt\ngit add product-from-hook.txt\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            self._git(["config", "core.hooksPath", str(hooks)], repo)

            upgrade.commit(repo, "TASK-78", "maintenance")

            self.assertFalse((repo / "product-from-hook.txt").exists())
            committed = self._git(["show", "--pretty=", "--name-only", "HEAD"], repo).splitlines()
            self.assertNotIn("product-from-hook.txt", committed)

    def test_ambient_git_execution_and_identity_overrides_are_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            external_diff = Path(directory) / "external-diff"
            marker = Path(directory) / "external-diff-ran"
            external_diff.write_text(
                f"#!/bin/sh\ntouch {marker}\n",
                encoding="utf-8",
            )
            external_diff.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_EXTERNAL_DIFF": str(external_diff),
                    "GIT_AUTHOR_NAME": "Injected Author",
                    "GIT_AUTHOR_EMAIL": "injected@example.invalid",
                    "GIT_COMMITTER_NAME": "Injected Committer",
                    "GIT_COMMITTER_EMAIL": "injected@example.invalid",
                },
                clear=False,
            ):
                upgrade.commit(repo, "TASK-78", "maintenance")

            self.assertFalse(marker.exists())
            self.assertEqual("Test User", self._git(["show", "-s", "--format=%an", "HEAD"], repo))
            self.assertEqual("Test User", self._git(["show", "-s", "--format=%cn", "HEAD"], repo))

    def test_ordinary_task_commit_still_rejects_automation_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nautomation_core = ["Justfile", ".automation/**"]\nsecret_patterns = []\n')
            self._write_file(repo / "Justfile", "changed\n")
            with mock.patch.object(agent_core, "ensure_task_branch", return_value="task/TASK-78-maintenance"), \
                    mock.patch.object(agent_core, "pending_paths", return_value=["Justfile"]), \
                    mock.patch.object(agent_core, "run") as run:
                with self.assertRaisesRegex(agent_core.AutomationError, "Automation Core"):
                    agent_core.commit_task(repo, "TASK-78", "ordinary")
            run.assert_not_called()

    def test_maintenance_environment_alone_is_not_commit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nautomation_core = []\nsecret_patterns = []\n')
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                error = self._commit_error(repo)
            self.assertIn("no active successful automation upgrade receipt", error)

    def test_receipt_rejects_product_adapter_repository_secret_and_task_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nsecret_patterns = ["secret"]\n')
            for path in ("product.txt", ".automation/ADAPTER", "just/project/mod.just", ".automation/secret.json", ".task-state/task.md"):
                with self.subTest(path=path):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.receipt_paths(repo, {"changed_paths": [path]})

    def test_receipt_rejects_missing_extra_and_identity_mismatched_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            original = self._receipt(repo)
            original["changed_paths"] = original["changed_paths"][:-1]
            original["path_fingerprints"].pop(sorted(self._receipt(repo)["changed_paths"])[-1])
            upgrade.atomic_json_write(upgrade.receipt_path(repo), original)
            self.assertIn("pending paths do not exactly match", self._commit_error(repo))

            receipt = self._receipt(repo)
            self._write_file(repo / ".automation/extra", "extra\n")
            self.assertIn("pending paths do not exactly match", self._commit_error(repo))

    def test_receipt_rejects_wrong_task_branch_worktree_stale_head_and_fingerprint(self) -> None:
        for field, value, expected in (
            ("task_id", "OTHER", "identity"),
            ("branch", "task/TASK-78-other", "identity"),
            ("worktree", "/other/worktree", "identity"),
            ("authority_head", "0" * 40, "HEAD"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                repo, _ = self._apply_fixture(Path(directory))
                receipt = self._receipt(repo)
                receipt[field] = value
                upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
                self.assertIn(expected, self._commit_error(repo))

        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            path = self._receipt(repo)["changed_paths"][0]
            target = repo / Path(*path.split("/"))
            target.write_bytes(target.read_bytes() + b"changed")
            self.assertIn("fingerprint changed", self._commit_error(repo))
            target.chmod(target.stat().st_mode ^ 0o100)
            # Restore content, then mode alone must still invalidate the receipt.
            target.write_bytes(target.read_bytes()[:-7])
            self.assertIn("fingerprint changed", self._commit_error(repo))

    def test_task_state_identity_mismatches_are_rejected(self) -> None:
        for replacement, expected in (
            ("- Task ID: OTHER", "does not match requested Task"),
            ("- Branch: task/OTHER-maintenance", "not the Task branch"),
            ("- Worktree: /other/worktree", "does not match the current worktree"),
        ):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                repo, _ = self._apply_fixture(Path(directory))
                state = repo / ".task-state/task.md"
                text = state.read_text(encoding="utf-8")
                label, value = replacement.split(": ", 1)
                lines = [line if not line.startswith(label) else replacement for line in text.splitlines()]
                state.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self.assertIn(expected, self._commit_error(repo))

        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            state = repo / ".task-state/task.md"
            state.write_text(
                state.read_text(encoding="utf-8").replace(
                    "- Branch: task/TASK-78-maintenance", "- Branch: task/TASK-78-unregistered"
                ),
                encoding="utf-8",
            )
            self.assertIn("not registered", self._commit_error(repo))

    def test_missing_consumed_receipt_and_cached_diff_failure_restore_active_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            receipt = self._receipt(repo)
            upgrade.receipt_path(repo).unlink()
            self.assertIn("no active", self._commit_error(repo))
            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            upgrade.atomic_json_write(upgrade.consumed_receipt_path(repo), receipt | {"status": "consumed"})
            upgrade.receipt_path(repo).unlink()
            self.assertIn("no active", self._commit_error(repo))

            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            path = receipt["changed_paths"][0]
            target = repo / Path(*path.split("/"))
            original_bytes = target.read_bytes()
            target.write_bytes(original_bytes.rstrip(b"\n") + b"  \n")
            updated = self._receipt(repo)
            updated["path_fingerprints"][path] = upgrade.file_fingerprint(repo, path)
            upgrade.atomic_json_write(upgrade.receipt_path(repo), updated)
            upgrade.write_authority(repo, updated)
            self.assertIn("git diff --no-ext-diff --cached --check", self._commit_error(repo))
            target.write_bytes(original_bytes)
            self._git(["reset", "--mixed", "HEAD"], repo)
            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            upgrade.write_authority(repo, receipt)
            original_run = upgrade.run

            def fake_run(command, *, cwd, **kwargs):
                if command == [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--cached",
                    "--name-only",
                ]:
                    return subprocess.CompletedProcess(command, 0, stdout="AGENTS.md\nextra\n", stderr="")
                return original_run(command, cwd=cwd, **kwargs)

            with mock.patch.object(
                upgrade, "pending_paths", return_value=receipt["changed_paths"]
            ), mock.patch.object(upgrade, "run", side_effect=fake_run):
                self.assertIn("staged paths", self._commit_error(repo))
            self.assertTrue(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.consumed_receipt_path(repo).exists())

    def test_source_revision_is_null_for_non_git_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source = self._apply_fixture(Path(directory), source_git=False)
            self.assertIsNone(self._receipt(repo)["source_revision"])


if __name__ == "__main__":
    unittest.main()
