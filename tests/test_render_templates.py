from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from tools.render_templates import (
    CompositionError,
    check_template,
    load_manifest,
    render_template,
    snapshot,
)


class RenderTemplatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "components" / "agent-core").mkdir(parents=True)
        (self.root / "components" / "adapters" / "base").mkdir(parents=True)
        (self.root / "templates").mkdir()
        self.write_manifest()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(self) -> None:
        (self.root / "templates" / "manifest.json").write_text(
            json.dumps(
                {
                    "templates": {
                        "agent-base": {
                            "adapter": "base",
                            "description": "base",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def spec(self):
        return load_manifest(self.root)["agent-base"]

    def test_render_preserves_dotfiles_and_executable_bit(self) -> None:
        core = self.root / "components" / "agent-core" / ".automation"
        core.mkdir()
        (core / "config").write_text("core\n", encoding="utf-8")
        executable = self.root / "components" / "adapters" / "base" / "run.sh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        output = render_template(self.root, self.spec())

        self.assertEqual((output / ".automation" / "config").read_text(), "core\n")
        self.assertTrue(stat.S_IMODE((output / "run.sh").stat().st_mode) & 0o111)

    def test_collision_fails_instead_of_overwriting(self) -> None:
        (self.root / "components" / "agent-core" / "shared").write_text("core")
        (self.root / "components" / "adapters" / "base" / "shared").write_text("adapter")

        with self.assertRaisesRegex(CompositionError, "path collision"):
            render_template(self.root, self.spec())

    def test_render_is_idempotent(self) -> None:
        (self.root / "components" / "agent-core" / "core.txt").write_text("core")
        (self.root / "components" / "adapters" / "base" / "adapter.txt").write_text("adapter")

        output = render_template(self.root, self.spec())
        first = snapshot(output)
        output = render_template(self.root, self.spec())
        second = snapshot(output)

        self.assertEqual(first, second)

    def test_check_detects_drift(self) -> None:
        (self.root / "components" / "agent-core" / "core.txt").write_text("core")
        output = render_template(self.root, self.spec())
        self.assertEqual(check_template(self.root, self.spec()), [])

        (output / "core.txt").write_text("changed")
        self.assertEqual(check_template(self.root, self.spec()), ["changed: core.txt"])


if __name__ == "__main__":
    unittest.main()
