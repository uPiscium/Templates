# Agent Core automation

This directory contains the language-independent Task lifecycle, publication,
and integration layer shared by every Agent-ready template.

Public operations are exposed through the top-level Just modules rather than by
calling these scripts directly. Project-specific build, lint, test, and toolchain
behavior belongs under `just/project/` in the selected Project Adapter.

Task Orchestrators use the persisted Work Unit API: select with
`work-unit-next`, create with `work-unit-create`, and verify dispatch with
`work-unit-dispatch-check`. These Task-local APIs are denied globally and
allowed only to the Task Orchestrator. Leaf completion is limited to the four
canonical statuses; failed evidence requires a fresh corrective Work Unit.

The current implementation provides guarded Task-local commit/push/PR operations,
integration head-SHA checkpoints, disposable Task State templates, and common
safety policy. Repository-local OpenCode agents and permissions are added by the
separate OpenCode configuration work.

## Automation Maintenance workflow

An Agent Core upgrade is permitted only from a dedicated registered, non-default
Automation Maintenance Task. From that Task worktree, use a trusted local
Templates checkout as the source:

```sh
AUTOMATION_MAINTENANCE=1 just automation::upgrade <trusted local Templates checkout>
```

The source must be a trusted clean Git worktree root with a full, non-null
`HEAD`. Both `automation::check-update` and `automation::upgrade` reject
tracked modifications and non-ignored untracked paths under
`components/agent-core`; ignored generated artifacts are structurally absent.
They pin the source `HEAD`, materialize only tracked Agent Core objects into a
temporary snapshot, and plan/copy only from that snapshot. Compatible
`VERSION` drift remains detectable, while a source race fails closed.

The environment variable is an upgrade opt-in only. It does not authorize a
commit; ordinary `just agent::commit <task>` rejects Automation Core changes.
Upgrade does not commit, push, or merge and writes the ignored receipt
`.task-state/automation-maintenance.json`.

Before publication, execute `git diff --check`, `just agent::doctor`,
`just project::check`, and the repository CI/smoke suite. Then publish only with:

```sh
just automation::commit <task> [message]
just agent::push <task>
just agent::pr-create <task>
```

The normal upgrade flow creates its receipt before verification. If a self-hosted
pre-receipt upgrade leaves the exact upgraded diff without that receipt, perform
normal verification and, before `automation::commit`, run the canonical recovery
bridge:

```sh
AUTOMATION_MAINTENANCE=1 just automation::bootstrap-receipt <trusted clean Git Templates checkout>
```

The bridge supports exactly two strict cases: canonical pre-receipt
reconstruction, and recovery of an exact active receipt whose authority is
missing. It does not trust `NO_CHANGES`, the current diff, or the environment.
It uses the same pinned clean-source and tracked Agent Core snapshot semantics as
the normal update operations, reruns pinned clean-source canonical
reconstruction, and requires Task/branch/worktree/`HEAD`, the pinned source
revision, and exact pending safe paths/content/modes and fingerprints. Recovery
additionally requires receipt exact equality and that it is unchanged, then
issues only the missing authority. A receipt with authority, or stale, forged,
or tampered state, fails closed. Product, Adapter, repository, secret-pattern,
or `.task-state` paths also fail closed. Continue with the existing
`automation::commit` flow; ordinary `agent::commit` rejection remains unchanged.

The receipt is schema-1 JSON containing Task identity (`task_id`, `branch`, and
`worktree`), source/source revision, current/upstream versions, sorted unique
`changed_paths`, `authority_head`, and exact per-path content/state
`path_fingerprints`. Commit fails closed if the receipt is absent, malformed,
stale, from another Task/worktree, has a different `HEAD`, changed fingerprints,
or does not exactly equal all pending paths. Receipt and authority publication
is a logical pair. Authority records live under the Git-resolved per-worktree
administrative directory returned by `--absolute-git-dir`, not an assumed
visible `.git` or shared Git directory; linked and special administrative
topologies are supported, and worktrees do not share authority. Existing safe
legacy shared-common-dir hashed records remain validation/commit compatible. A
protected record binds it to the preceding successful upgrade; a fabricated
Task State receipt is not authority. Ambient Git repository/index overrides are
scrubbed. The exact paths are staged and rechecked in a private Task State
index, then the commit is created from that verified tree without hooks and the
expected Task branch HEAD is advanced atomically. Only Agent Core-managed paths
are accepted; mixed Adapter, repository, product, secret-pattern, or
`.task-state` scopes are rejected. A handled authority-write failure removes the
newly written receipt if it is unchanged; an interruption half-state is
recoverable only through the strict bootstrap path. No cross-filesystem
atomicity is claimed. After a successful commit it is consumed at
`.task-state/automation-maintenance.consumed.json`; a subsequent successful
upgrade with changes replaces the active receipt and removes the previous consumed
receipt. A no-change invocation returns `NO_CHANGES` without discarding existing
receipt lifecycle evidence.

Do not bypass these guards with raw Git/GitHub commands. Merge is not part of
this workflow and remains the separately gated Main Orchestrator operation.
