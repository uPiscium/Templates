#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adopt_repository import AdoptionError, available_adapters, repository_root, select_adapter
from render_templates import TemplateSpec, compose_entries


def plan(source: Path, target: Path, requested: str) -> dict:
    root = repository_root(target)
    marker = root / ".automation" / "ADAPTER"
    if not marker.is_file():
        raise AdoptionError("repository is not Agent-ready: missing .automation/ADAPTER")
    current = marker.read_text(encoding="utf-8").strip()
    adapters = available_adapters(source)
    if current not in adapters:
        raise AdoptionError(f"current adapter {current!r} is unavailable in Templates")
    selected, reason, candidates = select_adapter(source, root, requested)
    if selected == current:
        return {
            "repositoryRoot": str(root),
            "fromAdapter": current,
            "toAdapter": selected,
            "adapterSelectionReason": reason,
            "adapterCandidates": candidates,
            "actions": [],
            "blockers": ["selected adapter is already active"],
            "canMigrate": False,
        }

    current_entries = compose_entries(
        source, TemplateSpec("migration-current", current, "current adapter")
    )
    target_entries = compose_entries(
        source, TemplateSpec("migration-target", selected, "target adapter")
    )

    actions: list[dict] = []
    blockers: list[str] = []
    paths = sorted(
        {
            path
            for path, entry in current_entries.items()
            if entry.component.startswith("adapter:")
        }
        | {
            path
            for path, entry in target_entries.items()
            if entry.component.startswith("adapter:")
        },
        key=lambda path: path.as_posix(),
    )

    for relative in paths:
        rel = relative.as_posix()
        destination = root / relative
        current_entry = current_entries.get(relative)
        target_entry = target_entries.get(relative)
        current_owned = current_entry is not None and current_entry.component.startswith("adapter:")
        target_owned = target_entry is not None and target_entry.component.startswith("adapter:")

        if rel == ".automation/ADAPTER" and target_owned:
            actions.append(
                {
                    "path": rel,
                    "action": "replace-adapter",
                    "reason": f"switch adapter marker {current} -> {selected}",
                }
            )
            continue

        if current_owned and destination.exists():
            if not destination.is_file() or not current_entry.source.is_file():
                detail = "current adapter-owned path is not a regular file"
                blockers.append(f"{rel}: {detail}")
                actions.append({"path": rel, "action": "blocked", "reason": detail})
                continue
            if destination.read_bytes() != current_entry.source.read_bytes():
                detail = "repository modified a current adapter-owned file; automatic replacement is unsafe"
                blockers.append(f"{rel}: {detail}")
                actions.append({"path": rel, "action": "blocked", "reason": detail})
                continue

        if target_owned:
            action = "replace-adapter" if destination.exists() else "create-adapter"
            actions.append(
                {
                    "path": rel,
                    "action": action,
                    "reason": f"target adapter {selected} owns this path",
                }
            )
        elif current_owned:
            actions.append(
                {
                    "path": rel,
                    "action": "remove-adapter",
                    "reason": f"path belongs only to previous adapter {current}",
                }
            )

    return {
        "repositoryRoot": str(root),
        "fromAdapter": current,
        "toAdapter": selected,
        "adapterSelectionReason": reason,
        "adapterCandidates": candidates,
        "actions": actions,
        "blockers": blockers,
        "canMigrate": not blockers,
        "readOnly": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan Project Adapter migration")
    parser.add_argument("target", type=Path)
    parser.add_argument("--adapter", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    try:
        print(json.dumps(plan(source, args.target.resolve(), args.adapter), indent=2, sort_keys=True))
        return 0
    except AdoptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
