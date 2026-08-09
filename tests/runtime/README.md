# Repository-local runtime smoke harness

Runtime validation is executed from the `Templates` repository root, while all generated fixtures, OpenCode logs, session snapshots, and reports live under the Git-ignored `.runtime-smoke/` directory.

```text
Templates/
├── tests/runtime/                  # committed harness and procedures
├── just/runtime.just              # stable operator API
└── .runtime-smoke/                # local evidence; never committed
    └── issue-41/
        ├── smoke-repo/
        ├── smoke-origin.git/
        ├── logs/
        ├── evidence/
        ├── reports/REPORT.md
        └── metadata.json
```

The harness never writes runtime evidence into tracked repository paths. Do not move logs or session exports into the repository for convenience; summarize evidence in the relevant GitHub Issue instead.

## Prepare a fresh fixture

From the `Templates` root:

```sh
just runtime::doctor
just runtime::prepare issue-41 agent-python
just runtime::status issue-41
```

`runtime::prepare` creates a fresh generated repository from the current checkout, runs the documented `project::bootstrap`, initializes a local disposable Git origin, verifies the Main repository, and creates three deterministic Task worktrees with concrete Task State contracts:

- `SMOKE-CONTROL`: Ask-free nested leaf control
- `SMOKE-ASK`: Depth-2 approval/rejection probe
- `SMOKE-FALLBACK`: genuine usage-limit observation only

Preparation refuses to overwrite an existing `.runtime-smoke/<issue>/` workspace. Preserve existing evidence or remove the directory explicitly before requesting a genuinely fresh run.

## Direct leaf control

Run the same configured `general` leaf directly in the `SMOKE-CONTROL` worktree:

```sh
just runtime::direct-leaf issue-41
```

This uses non-interactive `opencode run --agent general --format json`, writes event and DEBUG logs under `.runtime-smoke/issue-41/logs/`, and applies a bounded diagnostic timeout. This is a provider/agent isolation control only; it is not evidence that Depth-2 delegation works.

## Ask-free nested control

```sh
just runtime::diagnose-child-stall issue-41
```

The command starts the real Main TUI with DEBUG logging. In the TUI, run:

```text
/task-run SMOKE-CONTROL
```

The Task contract requires the Task Orchestrator to delegate exactly one `general` leaf and for that leaf to run only `git status --short`.

Interpretation:

- direct leaf stalls: investigate provider/model or leaf-agent execution before nested orchestration
- direct leaf passes but nested control stalls: investigate Task/subagent session execution
- nested control passes: proceed to the Ask probe

## Depth-2 Ask probe

```sh
just runtime::smoke-depth2 issue-41
```

Then run:

```text
/task-run SMOKE-ASK
```

The leaf contract requests two harmless unclassified Bash commands in sequence. Approve the first once and reject the second. Do not use auto-approval and do not change repository permissions.

A child-session permission that can only be handled by navigating away from Main does not satisfy the centralized Main-TUI Ask requirement.

## Model fallback observation

```sh
just runtime::smoke-fallback issue-41
```

Then run:

```text
/task-run SMOKE-FALLBACK
```

Do not manufacture a quota condition. If no genuine usage/quota/rate-limit failure occurs, runtime fallback remains `INCOMPLETE`.

## Session evidence

The interactive launchers automatically snapshot the session list before and after each TUI run. Additional snapshots can be requested explicitly:

```sh
just runtime::sessions issue-41 after-control
```

Export only session IDs relevant to this diagnostic, using OpenCode's sanitized export path:

```sh
just runtime::export-session ses_... issue-41
```

Exports are written under `.runtime-smoke/issue-41/evidence/` and must remain untracked.

## Report

```sh
just runtime::report issue-41
```

Edit the returned `REPORT.md` in place. Record only observed evidence. An unexecuted item is `INCOMPLETE`, not `PASS`.

## Sensitive data

DEBUG logs are local diagnostic artifacts and may contain prompts, filesystem paths, provider errors, or other operational details. `.runtime-smoke/` is intentionally ignored. Do not commit or paste raw logs without reviewing them for credentials and sensitive content first.
