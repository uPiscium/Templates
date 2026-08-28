---
description: Create an authoritative Issue-backed Task branch, worktree, and Task State
agent: build
---

Create the explicitly supplied numeric GitHub Issue-backed Task with `just agent::task-start-from-issue <numeric-issue> <slug>`. The Issue in the current repository is authoritative; do not use an arbitrary URL, repository, low-level `task-start`, placeholder Task, or interpretation step.

Run this only from the default-branch worktree. Run `just agent::contract-check <numeric-issue>` and require `status: READY` before launching exactly one Task Orchestrator. Do not implement the Task in the Main Orchestrator. After creation and the READY check, report the resolved branch and worktree and stop unless the user also asked to run the Task.
