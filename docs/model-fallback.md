# Model fallback

Agent-ready repositories use an explicit role-scoped model fallback policy at `.automation/model-fallback.toml`.

## Why this is controlled rather than transparent

OpenCode does not currently expose first-class cross-model failover for an active agent session. The upstream request for native model fallback is still open. OpenCode also treats some retryable provider errors as retry states, so an unbounded automatic retry loop would be unsafe.

For that reason, this repository implements fallback as explicit alternate agent definitions. Each fallback agent keeps the same role, permission boundary, Work Unit, scope, and authority while changing only the configured model.

## Automatic subagent fallback

Automatic fallback is allowed only when the failed invocation is classified as a usage/quota/rate-limit failure, such as HTTP 429, `rate_limit_exceeded`, quota exhaustion, or usage-limit exhaustion.

The parent agent retries the identical Task or Work Unit with the next named fallback agent in policy. Each configured model is attempted at most once. When the chain is exhausted, the Task becomes BLOCKED.

The following failures do not trigger automatic fallback:

- authentication or authorization failure
- permission denial
- invalid request or validation failure
- context-window exhaustion
- tool execution failure
- safety refusal
- unclassified failures

## Role-specific fallback split (Issue #47)

The final role split is:

- `task-orchestrator` → **Spark** primary, automatic fallback to **Sol** only for classified usage/quota/rate-limit failures.
- `general`, `explore`, `verifier` → **Luna** primary, automatic fallback to **Spark** only for classified usage/quota/rate-limit failures.
- `scout` → **Luna** primary, automatic fallback to **Terra** only for classified usage/quota/rate-limit failures.
- `reviewer`, `investigator`, `security-reviewer` remain as configured (`Terra` in the baseline allocation).

## Main Orchestrator

The active Main Orchestrator cannot be transparently replaced safely with the current OpenCode API. The policy therefore sets `roles.build.automatic = false` and provides `build-fallback` as a manual primary-agent fallback with the same authority.

This boundary should be revisited when OpenCode adds native cross-model fallback or provides a stable plugin API that can replace the active primary model without replaying prompts, duplicating tool calls, or corrupting session state.

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
