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
5. If the Task Orchestrator invocation fails with a usage/quota/rate-limit condition explicitly listed in `.automation/model-fallback.toml`, retry the identical Task once with the configured `task-orchestrator-fallback` variant.
6. Inside the Task, leaf Work Units may use the same controlled retry rule for their role-specific fallback variants. Do not fallback for authentication, permission, validation, context-window, tool, or safety errors.
7. Record every attempted fallback in Task State evidence. When a configured chain is exhausted, mark the Task BLOCKED instead of inventing another model.
8. Accept only evidence-backed completion: changed files, verification results, review results, commit/PR state, blockers, and unverified checks.
9. Stop at integration-pending. Do not merge from this skill.

Main Orchestrator fallback is not transparent: if the active `build` model hits a usage limit, switch manually to the explicitly configured `build-fallback` agent. OpenCode currently has no safe native cross-model failover for the already-active primary session.

Do not silently select another Issue or Task. Do not weaken permissions to work around a blocked approval or missing tool/model.
