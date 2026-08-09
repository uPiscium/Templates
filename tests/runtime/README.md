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

This starts a local loopback `opencode serve` endpoint on an ephemeral port, creates a session through `/session`, and sends a synchronous `/session/{sessionID}/message` request with `agent: general`. The request does not override the configured model. The launcher verifies that exactly one completed Bash tool call ran `git status --short`, writes JSON event output and DEBUG logs under `.runtime-smoke/issue-41/logs/`, and applies a bounded diagnostic timeout. This is a provider/agent isolation control only; it is not evidence that Depth-2 delegation works.

## Ask-free nested control

```sh
just runtime::diagnose-child-stall issue-41
```

The command starts the real Main TUI with DEBUG logging. In the TUI, run:

```text
/task-run SMOKE-CONTROL
```

DEBUG stderr is saved to the per-run log file without being printed over the TUI. Set `OPENCODE_RUNTIME_LIVE_LOGS=1` only when live DEBUG output is explicitly needed.

The Task contract requires the Task Orchestrator to delegate exactly one `general` leaf and for that leaf to run only `git status --short`.

Interpretation:

- direct leaf stalls: investigate provider/model or leaf-agent execution before nested orchestration
- direct leaf passes but nested control stalls: investigate Task/subagent session execution
- nested control passes: the provider path works; the native Ask canary may be run independently

## Depth-2 native Ask compatibility canary

```sh
just runtime::smoke-depth2 issue-41
```

Then run:

```text
/task-run SMOKE-ASK
```

`SMOKE-ASK` is an upstream compatibility canary for descendant permission relay. It is not a Templates release gate and does not determine #7 completion; the durable release gate is Leaf -> Depth-1 escalation tracked by #51.

The canary contract requires the Task Orchestrator to pass one exact bounded Work Unit to one `general` leaf: first `printf 'depth2-ask-approved\n'`, then `printf 'depth2-ask-rejected\n'`.

The leaf must call Bash for the first printf immediately, and it must wait for that permission resolution before making any second request. A `question` event before the first Bash permission means the run is non-deterministic for this harness and must be treated as **INCOMPLETE/invalid** for this diagnostic.

To keep the canary independent from production Leaf policy, `runtime::prepare` installs a diagnostic-only profile before committing the disposable fixture: `question` is denied, Bash defaults to deny, `git status --short` remains available for controls, and only the two exact canary `printf` commands use Ask. This profile does not change generated template source or production permissions.

Approve the first request once and reject the second. If the run is incomplete due to ordering/classification mismatch, stop and re-run `/task-run SMOKE-ASK` (new session) to collect a fresh, retryable observation.

Do not use auto-approval and do not change repository permissions.

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
