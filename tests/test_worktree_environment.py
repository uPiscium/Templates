from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "adapters" / "python" / "just" / "project" / "environment.py"
SPEC = importlib.util.spec_from_file_location("python_environment", SCRIPT)
assert SPEC and SPEC.loader
python_environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(python_environment)


class WorktreeEnvironmentTest(unittest.TestCase):
    def test_python_environment_rebinds_to_current_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            main = base / "main"
            task = base / "task"
            main.mkdir()
            task.mkdir()
            old = os.environ.copy()
            try:
                os.environ["VIRTUAL_ENV"] = str(main / ".venv")
                os.environ["UV_PROJECT_ENVIRONMENT"] = str(main / ".venv")
                os.environ["PIP_REQUIRE_VIRTUALENV"] = "0"
                os.environ["PATH"] = os.pathsep.join(
                    [str(main / ".venv" / "bin"), "/usr/bin", str(task / ".venv" / "bin")]
                )
                env = python_environment.sanitized_environment(task)
            finally:
                os.environ.clear()
                os.environ.update(old)

            self.assertNotIn("VIRTUAL_ENV", env)
            self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(task / ".venv"))
            self.assertEqual(env["PIP_REQUIRE_VIRTUALENV"], "1")
            self.assertNotIn(str(main / ".venv" / "bin"), env["PATH"].split(os.pathsep))
            self.assertNotIn(str(task / ".venv" / "bin"), env["PATH"].split(os.pathsep))

    def test_python_source_and_generated_environment_boundary_match(self) -> None:
        self.assertEqual(
            SCRIPT.read_bytes(),
            (ROOT / "templates" / "agent-python" / "just" / "project" / "environment.py").read_bytes(),
        )
        for relative in ("mod.just", "python.just", "lockfiles.py"):
            self.assertEqual(
                (ROOT / "components" / "adapters" / "python" / "just" / "project" / relative).read_bytes(),
                (ROOT / "templates" / "agent-python" / "just" / "project" / relative).read_bytes(),
            )

    def test_other_adapters_do_not_export_cross_worktree_stateful_paths(self) -> None:
        rust_flake = (ROOT / "components" / "adapters" / "rust" / "flake.nix").read_text(encoding="utf-8")
        nix_flake = (ROOT / "components" / "adapters" / "nix" / "flake.nix").read_text(encoding="utf-8")
        cpp_flake = (ROOT / "components" / "adapters" / "cpp-cmake" / "flake.nix").read_text(encoding="utf-8")
        self.assertNotIn("CARGO_TARGET_DIR", rust_flake)
        self.assertNotIn("CMAKE_BUILD_DIR", cpp_flake)
        self.assertNotIn("CMAKE_PRESET", cpp_flake)
        self.assertNotIn("CARGO_TARGET_DIR", nix_flake)


if __name__ == "__main__":
    unittest.main()
