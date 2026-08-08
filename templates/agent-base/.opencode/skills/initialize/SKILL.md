---
name: initialize
description: Run mandatory read-only repository and Task initialization checks before work begins
---

# Initialize

Use this skill before planning, editing, delegation, or project commands in a new primary-agent or Task-Orchestrator session.

1. Read `AGENTS.md` and `.automation/INIT.md`.
2. If `.task-state/task.md` exists in the current worktree, read it.
3. Run `just agent::doctor`; stop on failure.
4. Run `just agent::context`; retain the machine-readable context.
5. Run `just project::doctor`; stop on failure.
6. Confirm the baseline HEAD and Git status reported by context.
7. For a Task worktree, verify the Task Contract is concrete and internally consistent.
8. Return `INITIALIZED` with the resolved context only after all checks pass.

Initialization is read-only. Do not edit `AGENTS.md`, Task State, Automation Core, project files, Git refs, worktrees, or installed packages. Do not perform bootstrap or repair actions.
