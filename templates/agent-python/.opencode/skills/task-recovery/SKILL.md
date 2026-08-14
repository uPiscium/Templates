---
name: task-recovery
description: Explicit post-failure recovery for one existing Task and worktree
---

# Task recovery

This is the supported explicit post-failure recovery path, not transparent same-turn model failover.

1. Resolve the explicit TASK FAMILY, Task ID, and assigned worktree. Reject missing or ambiguous identity.
2. Call guarded `just agent::recovery-start <task> <family>` and then read-only `just agent::recovery-status <task>`; stop BLOCKED on unknown state.
3. Call read-only `just agent::recovery-route <task> task-orchestrator` and use the returned exact agent variant. Launch that Task Orchestrator variant directly with the complete recovery status as context and the same Task/worktree. Never launch an unavailable-family primary first.
4. For leaf recovery, select only an existing `in-flight` or `failed` Work Unit from recovery status and load it with `work-unit-status`. Next call `recovery-route`; only after it returns a non-BLOCKED selection may the orchestrator delegate the stored role/objective unchanged to that exact policy-selected primary or fallback agent. Never derive routing from a hardcoded role set, invent a model, or launder permissions. If no Work Unit exists because Task-Orchestrator startup failed before leaf delegation, this is Task-level recovery and no Work Unit may be fabricated.
5. Preserve role authority, scope, Task identity, worktree identity, and all escalation rules. Run the Task to `integration-pending`, not merge.
6. Require the selected Task Orchestrator to preserve each original Work Unit semantic/ID and call guarded `just agent::recovery-record <task> <requested-role> <work-unit-id> <semantic-sha256> <outcome>` after each recovered Work Unit. Unknown Work Unit IDs, role/digest mismatch, non-recoverable state, chain exhaustion, or unknown routing state is BLOCKED. Record exact evidence; never claim an unexecuted command or check passed.

`task-recover-clear` is the separate explicit guarded clear operation. Prompt-level retry remains best-effort and is not this workflow.
