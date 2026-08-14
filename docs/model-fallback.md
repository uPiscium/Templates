# Model fallback

Agent-ready repositories use an explicit role-scoped model fallback policy at `.automation/model-fallback.toml`.

## Why this is controlled rather than transparent

OpenCode does not currently expose first-class cross-model failover for an active agent session. The upstream request for native model fallback is still open. OpenCode also treats some retryable provider errors as retry states, so an unbounded automatic retry loop would be unsafe.

For that reason, this repository implements fallback as explicit alternate agent definitions. Each fallback agent keeps the same role, permission boundary, Work Unit, scope, and authority while changing only the configured model.

## Implemented policy and classification

The implemented fallback classification applies only when the failed invocation is a usage/quota/rate-limit failure, such as HTTP 429, `rate_limit_exceeded`, quota exhaustion, or usage-limit exhaustion. This classification does not provide unavailable automatic same-turn cross-model failover.

The policy classifies failures and names explicit alternate agent definitions. Prompt-level retry is best-effort and is not genuine automatic same-turn cross-model failover: OpenCode cannot safely replace an active session today. Each configured model is attempted at most once; when the chain is exhausted, the Task becomes BLOCKED.

The following failures do not trigger automatic fallback:

- authentication or authorization failure
- permission denial
- invalid request or validation failure
- context-window exhaustion
- tool execution failure
- safety refusal
- unclassified failures

## Role-specific fallback split

Fallbacks cross token-quota families. Codex 5.6 variants share quota, so a 5.6-primary role falls back to Spark rather than another 5.6 model. Spark-primary leaf roles fall back to Luna.

The role split is:

- `task-orchestrator` → **Spark** primary, explicit policy fallback to **Sol**.
- `general`, `explore`, `verifier`, `scout` → **Spark** primary, explicit policy fallback to **Luna**.
- `architect`, `reviewer`, `investigator`, `security-reviewer` → their existing 5.6 primary, explicit policy fallback to **Spark**.
- `build` → **Sol** primary, manual fallback to **Spark**.

## Main Orchestrator

The active Main Orchestrator cannot be transparently replaced safely with the current OpenCode API. The policy therefore sets `roles.build.automatic = false` and provides `build-fallback` as a manual primary-agent fallback with the same authority.

This boundary should be revisited when OpenCode adds native cross-model fallback or provides a stable plugin API that can replace the active primary model without replaying prompts, duplicating tool calls, or corrupting session state.

## Explicit post-failure recovery

`/task-recover TASK FAMILY` is the supported recovery workflow after a failure. It initializes Main, starts and inspects guarded recovery state, routes directly to the policy-selected orchestrator variant, and preserves the same Task, worktree, Work Unit semantic/ID, and role authority. A Spark-unavailable family skips its unavailable primary and routes directly to the selected general, explore, verifier, or scout fallback. The recovery orchestrator routes before every leaf delegation, records outcomes through guarded APIs, and runs to `integration-pending`. `/task-recover-clear` clears only an explicitly identified recovery state.

The assigned Task worktree's `.automation/model-fallback.toml` is authoritative for start, route, and evidence selection, even when Main invokes the guarded command. Main resolves and validates the registered Task worktree first; it never substitutes its own potentially newer policy for the Task's role/model contract. The guarded command executes only its caller worktree's trusted helper code and treats the target Task policy strictly as data, preventing a Task-local helper edit from becoming executable Main code. Every route call recomputes from the Task-scoped policy, so the Main and Task-worktree views remain identical for the versioned policy contract and a stale or tampered routing snapshot is not authoritative. Policy chains must keep the primary agent equal to the requested role and fallback agent equal to that role's statically configured `<role>-fallback` variant; arbitrary or nonexistent variants fail closed.

Policy model IDs are not evidence by themselves. Recovery validates every policy chain against the selected `.opencode/agents/<agent>.md` frontmatter in the Task worktree and, when Main launches recovery, against Main's executable agent definitions as well. Validation binds both the declared model and the ordered permission contract: primary/fallback permissions must match within each role, Task/Main agent permissions must match across worktrees, and the ordered project-level `opencode.json` permission configuration must also match. The validated Main executable root is persisted in recovery identity; route/record always execute that trusted Main helper while reading Task policy as data, and revalidate the complete Task/Main authority binding before every route response and before any Work Unit outcome/evidence mutation. A missing definition, post-start drift, or any model/authority mismatch is BLOCKED.

Leaf Work Units are durable before delegation in `.task-state/work-units.json`. The guarded `work-unit-register` API stores a stable ID, requested role, exact bounded objective, SHA-256 semantic digest, and lifecycle state. Recovery snapshots the existing `in-flight` or `failed` units when it starts; `failed` is an explicitly recoverable pre-terminal state, and only `recovery-record` may transition it to a recovery outcome. The API accepts only a unit in that immutable snapshot and rejects unknown IDs, role/digest mismatches, reopened terminal units, and other non-recoverable states before updating the durable outcome. Ordinary Work Unit transitions are monotonic, so completed or blocked work cannot be relabeled as recoverable. If the Task Orchestrator itself failed before any leaf registration, recovery remains Task-level and does not invent a Work Unit.

Recovery activation is an explicit operator assertion that the named family is unavailable because of a genuine usage-limit observation; the repository cannot authenticate provider telemetry after a stopped OpenCode turn. Starting recovery never converts an artificial fixture or operator assertion into genuine runtime PASS evidence. The deterministic contract harness validates the recovery control path only; it is not genuine runtime verification. Native future migration should replace this explicit path only when OpenCode provides safe session-preserving cross-model failover.

## Evidence

Task-level fallback records are appended under `### Model fallback` in `.task-state/task.md`. Evidence includes role, failed model, fallback model, classified reason, and outcome. Provider credentials and secret values must never be recorded.

## Upstream migration

When OpenCode adds native ordered model fallback:

1. confirm it preserves agent permissions and current worktree/session identity;
2. map `.automation/model-fallback.toml` chains to the native configuration;
3. retain error classification and Task State evidence requirements;
4. remove fallback agent variants only after equivalent smoke tests pass;
5. keep manual fallback as a compatibility path until generated repositories are upgraded.

Upstream references:

- https://github.com/anomalyco/opencode/issues/7602
- https://github.com/anomalyco/opencode/issues/21960
