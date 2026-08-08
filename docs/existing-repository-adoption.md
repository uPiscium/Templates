# Existing repository adoption

Agent-ready repositories always have a Project Adapter. A repository without a dedicated adapter uses the `base` Adapter rather than entering an adapter-less state.

## Commands

From the Templates repository:

```sh
just template::adopt-plan /path/to/repository
just template::adopt-plan /path/to/repository base
just template::adopt-apply /path/to/repository base
just template::adapter-migrate-plan /path/to/repository cpp-cmake
```

`adopt-plan` is read-only. It reports the selected Adapter, selection reason, Agent Core version, dirty-state status, planned file actions, and blockers.

`adopt-apply` performs no commit, push, or merge. It refuses to run when the target working tree is dirty or when the plan contains unresolved collisions.

## Adapter selection

Explicit `--adapter <id>` always wins when the Adapter exists.

Auto selection uses only dedicated Adapters that are actually present in the current Templates source. Known marker examples are `CMakeLists.txt`, `pyproject.toml`, `Cargo.toml`, and `flake.nix`.

When no dedicated Adapter matches, or more than one dedicated Adapter matches, selection falls back to `base`. It does not guess which dedicated Adapter the repository intended to use.

## Ownership and collisions

Adoption classifies each generated path before changing the repository.

- `create`: path is absent and may be materialized.
- `noop`: existing content already matches.
- `preserve`: the Adapter adoption policy leaves the repository-owned file unchanged.
- `merge`: a specifically defined safe merge strategy exists.
- `blocked`: non-identical content has no safe merge strategy.

The base Adapter preserves existing `flake.nix` and `flake.lock` files. It line-merges `/.worktrees/` into `.gitignore`.

Existing `Justfile` content is retained and only non-conflicting Agent Core module declarations are appended. Existing `AGENTS.md` content is retained and Agent Core rules are appended inside explicit markers.

Existing non-identical `opencode.json`, `.automation/**`, or other Automation Core-owned paths are not silently overwritten. They block adoption until the collision is resolved intentionally.

## Base Adapter

The base Adapter is the minimum Project Adapter contract for unknown repositories. It provides the stable `project::*` API without inventing project-specific verification:

```text
project::doctor        PASS
project::format-check  SKIPPED
project::lint          SKIPPED
project::test          SKIPPED
project::build         SKIPPED
project::check         PASS
```

A skipped project-specific operation remains explicitly `SKIPPED`; it is not represented as work that actually ran.

After adoption, the repository contains the normal Agent Core version, Adapter marker, initialization contract, OpenCode agents, guarded Just API, and worktree lifecycle. The normal read-only `/init` contract applies immediately.

## Migration to a dedicated Adapter

`adapter-migrate-plan` is read-only. It compares the currently active Adapter with the requested target Adapter.

A path currently owned by the active Adapter is replaceable only when the repository copy still matches that Adapter's source. If the repository modified an Adapter-owned path, migration reports a blocker instead of overwriting it.

Agent Core files are not replaced by Adapter migration. Repository extensions are outside Adapter ownership and remain protected.

Actual Adapter migration application is intentionally separate from planning so that future dedicated Adapters can define their ownership and migration rules without weakening the collision policy.
