#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

PLACEHOLDER = "@@PROJECT_NAME@@"
DEFAULT_NAME = "smoke-project"


def replace_placeholder(root: Path, name: str) -> list[str]:
    changed: list[str] = []
    for relative in ("pyproject.toml", "Cargo.toml", "CMakeLists.txt", "README.md"):
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if PLACEHOLDER not in text:
            continue
        path.write_text(text.replace(PLACEHOLDER, name), encoding="utf-8")
        changed.append(relative)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an instantiated template for CI smoke checks")
    parser.add_argument("root", type=Path)
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args()
    changed = replace_placeholder(args.root.resolve(), args.name)
    print("prepared: " + (", ".join(changed) if changed else "no placeholders"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
