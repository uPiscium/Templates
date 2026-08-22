from __future__ import annotations

import unittest
from pathlib import Path

from tools.render_templates import check_template, load_manifest

ROOT = Path(__file__).resolve().parents[1]


class LanguageAdapterTest(unittest.TestCase):
    def test_manifest_registers_python_and_rust(self) -> None:
        specs = load_manifest(ROOT)
        self.assertEqual(specs["agent-python"].adapter, "python")
        self.assertEqual(specs["agent-rust"].adapter, "rust")

    def test_generated_language_templates_have_no_source_drift(self) -> None:
        specs = load_manifest(ROOT)
        self.assertEqual(check_template(ROOT, specs["agent-python"]), [])
        self.assertEqual(check_template(ROOT, specs["agent-rust"]), [])

    def test_python_adapter_preserves_stable_contract(self) -> None:
        adapter = ROOT / "components" / "adapters" / "python"
        project = (adapter / "just" / "project" / "mod.just").read_text(encoding="utf-8")
        for recipe in ("doctor:", "format-check:", "lint:", "test:", "build:", "check:"):
            self.assertIn(recipe, project)
        for tool in ("uv", "ruff", "mypy", "pytest"):
            self.assertIn(tool, project)
        self.assertIn("src tests", project)
        self.assertIn("SKIPPED", project)
        self.assertIn("lockfiles.py", project)
        self.assertLess(project.index("project_bootstrap.py"), project.index("lockfiles.py"))
        self.assertEqual(project.count("uv run --locked"), 4)
        self.assertNotIn("exec uv run ruff", project)
        self.assertNotIn("exec uv run mypy", project)
        self.assertNotIn("exec uv run pytest", project)

        python = (adapter / "just" / "project" / "python.just").read_text(encoding="utf-8")
        self.assertEqual(python.count("uv run --locked"), 3)
        self.assertIn("exec uv sync", python)

        pre_commit = (adapter / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        self.assertEqual(pre_commit.count("uv run --locked"), 2)
        self.assertNotIn("entry: uv run ruff", pre_commit)

        flake = (adapter / "flake.nix").read_text(encoding="utf-8")
        for tool in ("uv", "just", "git", "gh", "jq", "ruff", "mypy"):
            self.assertIn(tool, flake)
        self.assertIn("UV_PROJECT_ENVIRONMENT=$PWD/.venv", flake)
        self.assertIn("PIP_REQUIRE_VIRTUALENV=1", flake)

    def test_rust_adapter_preserves_stable_contract(self) -> None:
        adapter = ROOT / "components" / "adapters" / "rust"
        project = (adapter / "just" / "project" / "mod.just").read_text(encoding="utf-8")
        for recipe in ("doctor:", "format-check:", "lint:", "test:", "build:", "check:"):
            self.assertIn(recipe, project)
        self.assertIn("cargo fmt --all -- --check", project)
        self.assertIn("cargo clippy --all-targets --all-features -- -D warnings", project)
        self.assertIn("cargo test --all-targets --all-features", project)
        self.assertIn("cargo build --all-targets --all-features", project)
        self.assertIn("cargo clippy --version", project)

        flake = (adapter / "flake.nix").read_text(encoding="utf-8")
        for tool in ("cargo", "rustc", "rustfmt", "clippy", "rust-analyzer", "just", "git", "gh", "jq"):
            self.assertIn(tool, flake)

    def test_public_flake_names_are_compatibility_aliases(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertGreaterEqual(flake.count("path = ./templates/agent-python;"), 2)
        self.assertGreaterEqual(flake.count("path = ./templates/agent-rust;"), 2)
        self.assertIn("agent-python", flake)
        self.assertIn("agent-rust", flake)

    def test_generated_roots_use_agent_core_router(self) -> None:
        for name in ("agent-python", "agent-rust"):
            justfile = (ROOT / "templates" / name / "Justfile").read_text(encoding="utf-8")
            self.assertIn("mod agent '.automation/just/agent.just'", justfile)
            self.assertIn("mod project 'just/project/mod.just'", justfile)
            self.assertNotIn("check-all:", justfile)


if __name__ == "__main__":
    unittest.main()
