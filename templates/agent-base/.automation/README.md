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
