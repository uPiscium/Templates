from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CppCMakeAdapterContractTest(unittest.TestCase):
    def test_manifest_and_generated_adapter(self) -> None:
        manifest = json.loads((ROOT / "templates" / "manifest.json").read_text())
        self.assertEqual(manifest["templates"]["agent-cpp-cmake"]["adapter"], "cpp-cmake")
        self.assertEqual(
            (ROOT / "templates" / "agent-cpp-cmake" / ".automation" / "ADAPTER").read_text().strip(),
            "cpp-cmake",
        )

    def test_stable_project_api_and_submodules(self) -> None:
        text = (ROOT / "components" / "adapters" / "cpp-cmake" / "just" / "project" / "mod.just").read_text()
        for recipe in ("doctor:", "configure:", "format-check:", "lint:", "test:", "build:", "check:"):
            self.assertIn(recipe, text)
        for module in ("cmake", "quality", "tests"):
            self.assertIn(f"mod {module} ", text)
        self.assertIn("lint: configure quality::lint", text)
        self.assertIn("build: configure cmake::build", text)
        self.assertIn("test: build tests::all", text)

    def test_doctor_uses_configured_cxx_with_generic_fallback(self) -> None:
        for adapter in (
            ROOT / "components" / "adapters" / "cpp-cmake",
            ROOT / "templates" / "agent-cpp-cmake",
        ):
            project = (adapter / "just" / "project" / "mod.just").read_text(encoding="utf-8")
            self.assertIn('compiler="${CXX:-c++}"', project)
            self.assertIn('command -v "$compiler"', project)
            self.assertNotIn("command -v clang++", project)

    def test_doctor_honors_configured_cxx_and_empty_value_fallback(self) -> None:
        just = shutil.which("just")
        self.assertIsNotNone(just, "just is required for the runtime adapter test")
        assert just is not None

        template = ROOT / "templates" / "agent-cpp-cmake"
        for cxx, compiler in (("selected-cxx", "selected-cxx"), (None, "c++"), ("", "c++")):
            with self.subTest(CXX=cxx), tempfile.TemporaryDirectory() as directory:
                fake_bin = Path(directory)
                (fake_bin / "sh").symlink_to("/bin/sh")
                tools = ("cmake", "ninja", "clang-format", "clang-tidy", "ctest", compiler)
                for tool in tools:
                    shim = fake_bin / tool
                    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    shim.chmod(0o755)

                environment = os.environ.copy()
                environment["PATH"] = str(fake_bin)
                if cxx is None:
                    environment.pop("CXX", None)
                else:
                    environment["CXX"] = cxx
                result = subprocess.run(
                    [just, "project::doctor"],
                    cwd=template,
                    env=environment,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_worktree_local_build_and_no_clean_api(self) -> None:
        preset = json.loads((ROOT / "components" / "adapters" / "cpp-cmake" / "CMakePresets.json").read_text())
        self.assertEqual(preset["configurePresets"][0]["binaryDir"], "${sourceDir}/.build/default")
        cmake = (ROOT / "components" / "adapters" / "cpp-cmake" / "just" / "project" / "cmake.just").read_text()
        self.assertIn("${CMAKE_BUILD_DIR:-.build/default}", cmake)
        self.assertNotIn("--target clean", cmake)
        self.assertNotIn("rm -rf", cmake)

    def test_generated_source_parity(self) -> None:
        source = ROOT / "components" / "adapters" / "cpp-cmake"
        generated = ROOT / "templates" / "agent-cpp-cmake"
        for relative in (
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
            ".clang-format",
            ".clang-tidy",
            ".envrc",
            "CMakeLists.txt",
            "CMakePresets.json",
            "README.md",
            "flake.nix",
            "include/project/lib.hpp",
            "just/project/cmake.just",
            "just/project/mod.just",
            "just/project/quality.just",
            "just/project/repository.just",
            "just/project/test.just",
            "src/lib.cpp",
            "src/main.cpp",
            "tests/test_smoke.cpp",
        ):
            self.assertEqual((source / relative).read_bytes(), (generated / relative).read_bytes(), relative)


if __name__ == "__main__":
    unittest.main()
