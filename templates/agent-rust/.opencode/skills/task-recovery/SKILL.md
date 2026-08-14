---
name: task-recovery
description: Explicit post-failure recovery for one existing Task and worktree
---

# Task recovery

This is the supported explicit post-failure recovery path, not transparent same-turn model failover.

1. Resolve the explicit TASK FAMILY, Task ID, and assigned worktree. Reject missing or ambiguous identity.
2. Call guarded `just agent::recovery-start <task> <family>` and then read-only `just agent::recovery-status <task>`; stop BLOCKED on unknown state.
3. Call read-only `just agent::recovery-route <task> task-orchestrator` and use the returned exact agent variant. Launch that Task Orchestrator variant directly with the complete recovery status as context and the same Task/worktree. Never launch an unavailable-family primary first.
4. The selected orchestrator calls `recovery-route` before each leaf delegation. For Spark-unavailable families, route directly to the policy-selected general, explore, verifier, or scout fallback as applicable; do not invent a model or launder permissions.
5. Preserve role authority, scope, Task identity, worktree identity, and all escalation rules. Run the Task to `integration-pending`, not merge.
6. Require the selected Task Orchestrator to preserve each original Work Unit semantic/ID and call guarded `just agent::recovery-record <task> <requested-role> <work-unit-id> <outcome>` after each recovered Work Unit. Chain exhaustion or unknown state is BLOCKED. Record exact evidence; never claim an unexecuted command or check passed.

`task-recover-clear` is the separate explicit guarded clear operation. Prompt-level retry remains best-effort and is not this workflow.
