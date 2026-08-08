# Agent Operating Rules

This repository is designed for hierarchical agent-driven development.

## Durable invariants

- Before planning, editing, delegation, or project commands in a new primary-agent or Task-Orchestrator session, read `.automation/INIT.md` and complete the `initialize` skill. Initialization is read-only and failures block work.
- Use the repository-local Just API for Task lifecycle, publication, and integration operations.
- Do not bypass guarded Just recipes with raw state-changing Git or GitHub commands.
- Ordinary implementation Tasks must not modify Automation Core files: `opencode.json`, `AGENTS.md`, `Justfile`, `.opencode/**`, `.automation/**`, or `.github/workflows/**`.
- `flake.nix` and `flake.lock` may be modified only when the active Task explicitly includes environment or dependency changes.
- One Task owns one branch, one worktree, one disposable Task State, and one Task Orchestrator.
- Leaf agents execute bounded Work Units and never update `.task-state/task.md` directly.
- Leaf agents must not create subagents.
- Main Orchestrator owns Task scheduling and final integration. Task Orchestrators own implementation and publication preparation for exactly one Task.
- Task Orchestrators must not merge pull requests.
- A command or check that was not executed must never be reported as PASS.
- Do not substitute a different model ID when an explicitly configured model is unavailable. Use only explicitly configured fallback policy where applicable.

## Initialization layers

- `AGENTS.md`: durable repository rules.
- `.automation/INIT.md`: mandatory per-session read-only initialization sequence.
- `.task-state/task.md`: active Task contract, progress, and evidence.
- bootstrap: one-time state-changing repository creation/configuration, separate from `/init`.
- `/init`: read-only validation/context resolution only; it must not rewrite `AGENTS.md` or repair Automation Core.

## Agent call graph

```text
build
├── task-orchestrator
├── architect
├── reviewer
├── investigator
├── security-reviewer
└── scout

task-orchestrator
├── general
├── explore
├── verifier
├── reviewer
├── investigator
├── security-reviewer
└── scout

leaf agents
└── no further delegation
```

The call graph is intentionally non-cyclic. `task-orchestrator` may never invoke another `task-orchestrator`.

## Permission boundary

Automatically permitted operations are restricted to repository inspection, selected read-only Git/GitHub commands, safe initialization and `project::*` checks, Task-local commit, and constrained PR create/edit/ready operations through Just.

User approval is required for Task branch push, final merge, cleanup, `/tmp/opencode/**` access, and unclassified shell commands.

Raw Git/GitHub mutations, force push, amend, rebase, destructive reset/clean, direct default-branch push, admin merge, privilege escalation, and destructive store/filesystem operations are prohibited.

## External paths

The only generally requestable external path is `/tmp/opencode/**`, and it requires approval. Other paths outside the current OpenCode workspace are denied by default.

## Worktree isolation

Agents must operate only inside the worktree assigned to their current Task. Access to sibling Task worktrees is prohibited. Static OpenCode permissions provide the default boundary; Task/worktree lifecycle guards add the dynamic sibling-worktree checks.
