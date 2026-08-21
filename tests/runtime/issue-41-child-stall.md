# Issue #41 — OpenCode child-agent stall diagnosis

This procedure diagnoses where a native OpenCode child-agent run stops without weakening the repository permission model.

## Required order

Run controls in this order and stop guessing once one fails:

```text
1. direct `general` leaf
2. Main -> Task Orchestrator -> Ask-free `general` leaf
3. Main -> Task Orchestrator -> Ask-producing `general` leaf
```

All runs use the same generated `agent-python` fixture prepared by `just runtime::prepare issue-41 agent-python`.

## Stage model

Classify the furthest evidence-backed stage reached:

```text
Task tool launch
  -> child session creation
  -> provider request start
  -> provider response
  -> tool request
  -> permission.asked
  -> Main TUI relay
  -> tool execution
  -> child completion
```

Do not infer an intermediate stage from a spinner or child label alone.

## Native Ask canary ordering gate (retry requirement)

For `Control C`, classify deterministic ordering as follows:

- `permission.asked` (or equivalent) for `printf 'depth2-ask-approved\n'`
- provider execution result for the approved first command
- `permission.asked`/rejection for `printf 'depth2-ask-rejected\n'`

The prepared diagnostic fixture applies an explicit profile only to its generated `general` agent: `question` is denied, Bash defaults to deny, the read-only status control is allowed, and only the two exact canary `printf` commands use Ask. The configured model remains fixed with no substitution. This is independent from production Leaf policy and ensures future non-interactive Leaf changes do not disable the upstream compatibility check.

If the leaf emits a `question` event before the first Bash permission flow, treat the attempt as **INCOMPLETE/invalid** (not a relay failure), and rerun `just runtime::smoke-depth2 issue-41` + `/task-run SMOKE-ASK`.

## Control A — direct leaf

```sh
just runtime::direct-leaf issue-41
```

Expected diagnostic action: loopback API control sends a direct `general` session request that runs `git status --short` once without editing.

- PASS: provider responds, read-only command completes, process exits normally
- FAIL: explicit provider/tool/agent error is observed
- INCOMPLETE: bounded timeout expires without enough evidence

If this control does not pass, do not attribute the nested stall to permission relay.

## Control B — Ask-free nested leaf

```sh
just runtime::diagnose-child-stall issue-41
```

Inside Main TUI:

```text
/task-run SMOKE-CONTROL
```

Expected path:

```text
build -> task-orchestrator -> general -> git status --short -> return
```

No Ask event is required by this control.

- direct PASS + nested FAIL/INCOMPLETE: prioritize nested Task/session/provider execution
- direct PASS + nested PASS: nested provider path works; continue to Ask probe

## Control C — Depth-2 native Ask compatibility canary

This control observes upstream compatibility only. It is not a release blocker and does not determine #7 completion; Leaf -> Depth-1 escalation in #51 is the durable release gate.

```sh
just runtime::smoke-depth2 issue-41
```

Inside Main TUI:

```text
/task-run SMOKE-ASK
```

The leaf must request exactly these harmless commands in sequence, with the first command sent to Bash before any other tool and before any permission relay:

```sh
printf 'depth2-ask-approved\n'
printf 'depth2-ask-rejected\n'
```

Approve the first once and reject the second.

Do not mark this as permission-relay failure when `question` appears before the first Bash permission event; it should be recorded as INCOMPLETE and retried once for a clean ordering sample.

If provider response and tool request are visible but `permission.asked` or Main relay is absent, prioritize permission propagation. If no provider response occurs, keep the diagnosis in the provider/session path.

## Evidence collection

Interactive launchers write DEBUG stderr logs locally and take pre/post session-list snapshots. Record relevant session IDs, then export only those sessions:

```sh
just runtime::export-session <session-id> issue-41
```

Use the sanitized export as supporting evidence, not as a replacement for real TUI observation.

## Decision table

| Direct leaf | Nested Ask-free | Ask probe | Primary suspicion |
|---|---|---|---|
| stall | not run | not run | provider/model/direct leaf execution |
| pass | stall | not run | nested Task/subagent session execution |
| pass | pass | question before Bash permission | protocol mismatch → treat as INCOMPLETE/invalid and retry |
| pass | pass | stall before permission event | Ask-producing leaf/provider/tool request path |
| pass | pass | permission event exists but Main cannot handle it | permission relay/UI propagation |
| pass | pass | approve/reject both work | upstream compatibility restored; #7 remains gated by Leaf -> Depth-1 escalation |

## Non-negotiable constraints

- Do not change Ask to allow.
- Do not use OpenCode `--auto` for the permission smoke.
- Do not substitute configured models.
- Do not damage credentials or deliberately exhaust quota.
- Do not report a timeout as PASS.
- Do not commit `.runtime-smoke/` evidence.
