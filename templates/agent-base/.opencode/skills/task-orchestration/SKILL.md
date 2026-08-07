---
name: task-orchestration
description: Run one already-created Task through its Task Orchestrator without merging
---

# Task orchestration

Use this skill when the Main Orchestrator is asked to run a specific Task that already has a dedicated branch/worktree and Task State.

1. Resolve the explicit Task ID and assigned worktree.
2. Confirm the Task is not already owned by another active Task Orchestrator.
3. Launch exactly one `task-orchestrator` for that Task.
4. Require the Task Orchestrator to operate only in the assigned worktree and to use bounded Work Units.
5. Accept only evidence-backed completion: changed files, verification results, review results, commit/PR state, blockers, and unverified checks.
6. Stop at integration-pending. Do not merge from this skill.

Do not silently select another Issue or Task. Do not weaken permissions to work around a blocked approval or missing tool/model.
