from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import os
import shutil
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
SPEC = importlib.util.spec_from_file_location("automation_upgrade", SCRIPT)
assert SPEC and SPEC.loader
upgrade = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upgrade
SPEC.loader.exec_module(upgrade)


class AutomationUpgradeContractTest(unittest.TestCase):
    TEMPLATE_NAMES = ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake")

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
        self.assertIn("upgrade refused on default branch", script)
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
            with mock.patch.object(upgrade, "require_maintenance"):
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

            with mock.patch.object(upgrade, "require_maintenance"):
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
            with mock.patch.object(upgrade, "require_maintenance"):
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
            with mock.patch.object(upgrade, "require_maintenance"):
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
            with mock.patch.object(upgrade, "require_maintenance"):
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
            with mock.patch.object(upgrade, "require_maintenance"):
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

            with mock.patch.object(upgrade, "require_maintenance"):
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
            with mock.patch.object(upgrade, "require_maintenance"):
                with mock.patch.object(upgrade.shutil, "copy2", side_effect=shutil.copy2):
                    upgrade.apply(repo, tmp)
            self.assertEqual((repo / ".automation" / "ADAPTER").read_text(), "base\n")

    def test_current_v2_bridge_source_contains_no_delete_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            plan = upgrade.build_plan(repo, ROOT)
            self.assertFalse(any(item["action"] == "delete" for item in plan["actions"]))
            self.assertEqual([], upgrade.load_migrations(ROOT / "components/agent-core"))

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


if __name__ == "__main__":
    unittest.main()
