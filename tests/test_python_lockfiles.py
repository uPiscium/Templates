from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "components" / "adapters" / "python" / "just" / "project"


def load_lockfiles_module():
    environment_spec = importlib.util.spec_from_file_location("lockfiles_environment", PROJECT / "environment.py")
    assert environment_spec and environment_spec.loader
    environment = importlib.util.module_from_spec(environment_spec)
    environment_spec.loader.exec_module(environment)

    lockfiles_spec = importlib.util.spec_from_file_location("python_lockfiles", PROJECT / "lockfiles.py")
    assert lockfiles_spec and lockfiles_spec.loader
    lockfiles = importlib.util.module_from_spec(lockfiles_spec)
    with mock.patch.dict(sys.modules, {"environment": environment}):
        lockfiles_spec.loader.exec_module(lockfiles)
    return lockfiles


lockfiles = load_lockfiles_module()


class PythonLockfilesTest(unittest.TestCase):
    def test_missing_both_runs_nix_then_uv_and_creates_both(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)

            def run(command, *, cwd, env=None):
                self.assertEqual(cwd, repo)
                if command == ["nix", "flake", "lock"]:
                    (repo / "flake.lock").write_bytes(b"flake")
                elif command == ["uv", "lock"]:
                    (repo / "uv.lock").write_bytes(b"uv")
                else:
                    self.fail(f"unexpected command: {command}")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(lockfiles.subprocess, "run", side_effect=run) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 0)

            self.assertEqual(run_mock.call_args_list[0], mock.call(["nix", "flake", "lock"], cwd=repo))
            self.assertEqual(run_mock.call_args_list[1].args[0], ["uv", "lock"])
            self.assertEqual((repo / "flake.lock").read_bytes(), b"flake")
            self.assertEqual((repo / "uv.lock").read_bytes(), b"uv")

    def test_uv_receives_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "flake.lock").write_bytes(b"flake")
            (repo / "uv.lock").write_bytes(b"uv")
            with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/old/.venv", "UV_PROJECT_ENVIRONMENT": "/old"}):
                with mock.patch.object(lockfiles.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run_mock:
                    self.assertEqual(lockfiles.ensure(repo), 0)

            self.assertEqual(run_mock.call_count, 0)

            (repo / "uv.lock").unlink()
            with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/old/.venv", "UV_PROJECT_ENVIRONMENT": "/old"}):
                def run(command, *, cwd, env):
                    (cwd / "uv.lock").write_bytes(b"uv")
                    return SimpleNamespace(returncode=0)

                with mock.patch.object(lockfiles.subprocess, "run", side_effect=run) as run_mock:
                    self.assertEqual(lockfiles.ensure(repo), 0)

            env = run_mock.call_args.kwargs["env"]
            self.assertNotIn("VIRTUAL_ENV", env)
            self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(repo / ".venv"))
            self.assertEqual(env["PIP_REQUIRE_VIRTUALENV"], "1")

    def test_existing_both_arbitrary_bytes_are_preserved_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            flake_bytes = b"\x00flake\xff"
            uv_bytes = b"\x00uv\xfe"
            (repo / "flake.lock").write_bytes(flake_bytes)
            (repo / "uv.lock").write_bytes(uv_bytes)
            with mock.patch.object(lockfiles.subprocess, "run") as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 0)
            self.assertEqual(run_mock.call_count, 0)
            self.assertEqual((repo / "flake.lock").read_bytes(), flake_bytes)
            self.assertEqual((repo / "uv.lock").read_bytes(), uv_bytes)

    def test_existing_flake_missing_uv_runs_only_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "flake.lock").write_bytes(b"flake")

            def run(command, *, cwd, env):
                self.assertEqual(command, ["uv", "lock"])
                (cwd / "uv.lock").write_bytes(b"uv")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(lockfiles.subprocess, "run", side_effect=run) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 0)
            self.assertEqual(run_mock.call_count, 1)

    def test_missing_flake_existing_uv_runs_only_nix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "uv.lock").write_bytes(b"uv")
            def run(command, *, cwd):
                (cwd / "flake.lock").write_bytes(b"flake")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(lockfiles.subprocess, "run", side_effect=run) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 0)
            run_mock.assert_called_once_with(["nix", "flake", "lock"], cwd=repo)

    def test_nix_failure_returns_nonzero_and_skips_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            with mock.patch.object(lockfiles.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 7)
            run_mock.assert_called_once_with(["nix", "flake", "lock"], cwd=repo)
            self.assertFalse((repo / "uv.lock").exists())

    def test_uv_failure_returns_nonzero_without_fake_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "flake.lock").write_bytes(b"flake")
            with mock.patch.object(lockfiles.subprocess, "run", return_value=SimpleNamespace(returncode=9)) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 9)
            run_mock.assert_called_once()
            self.assertFalse((repo / "uv.lock").exists())

    def test_success_without_expected_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            with mock.patch.object(lockfiles.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                self.assertEqual(lockfiles.ensure_lockfile(repo, "flake.lock", ["nix", "flake", "lock"]), 1)

    def test_second_ensure_is_idempotent_and_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            contents = {"flake.lock": b"flake\x00\xff", "uv.lock": b"uv\x01\xfe"}

            def run(command, *, cwd, env=None):
                name = "flake.lock" if command[0] == "nix" else "uv.lock"
                (cwd / name).write_bytes(contents[name])
                return SimpleNamespace(returncode=0)

            with mock.patch.object(lockfiles.subprocess, "run", side_effect=run) as run_mock:
                self.assertEqual(lockfiles.ensure(repo), 0)
                first = {name: (repo / name).read_bytes() for name in contents}
                run_mock.reset_mock()
                self.assertEqual(lockfiles.ensure(repo), 0)
                second = {name: (repo / name).read_bytes() for name in contents}

            self.assertEqual(second, first)
            run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
