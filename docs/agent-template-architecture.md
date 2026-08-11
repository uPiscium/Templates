# Agent-ready Repository Template Architecture

Related: #4

## 1. Purpose

This document defines the stable architecture and public contracts for Agent-ready repository templates generated from this repository.

The goal is to make generated repositories self-contained execution environments for AI-assisted development while keeping orchestration, project tooling, Git publication, and integration responsibilities explicit and separable.

This document is a design contract for later implementation issues. It intentionally specifies structure, ownership, interfaces, lifecycle boundaries, and permission boundaries without prescribing all script internals.

## 2. Core invariants

1. One implementation Task owns exactly one branch, one Git worktree, one disposable Task State, and one Depth-1 Task Orchestrator session.
2. Multiple Tasks may run concurrently only when they use different worktrees and their dependencies and integration surfaces allow parallel execution.
3. A Task Orchestrator may delegate bounded Work Units to leaf agents, but leaf agents may not delegate again.
4. The Main Orchestrator owns repository-wide scheduling and integration decisions.
5. Task Orchestrators may prepare commits and pull requests, but they may not merge.
6. Merge is a Main Orchestrator operation and remains permission-gated.
7. Repository-local Just recipes are the stable automation API. Agents should not bypass an approved recipe with raw state-changing Git or GitHub commands.
8. Agent Core is independent of language and build system. Project Adapters implement language- and toolchain-specific behavior behind a stable `project::*` contract.
9. Session initialization is read-only. Repository bootstrap and Agent Core upgrades are separate state-changing workflows.
10. Task State is disposable execution state and must never be committed or pushed.
11. Depth-2 leaf agents are non-interactive and may return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
12. The Depth-1 Task Orchestrator is the escalation approval and decision boundary and must independently re-evaluate scope, authority, least-privilege alignment, safety, alternatives, and evidence before resolving any `NEEDS_APPROVAL`/`NEEDS_DECISION` result. An unresolved human decision is asked from Depth 1, not relayed by the Leaf.
13. Task Orchestrator must not automatically convert leaf denials into Ask, propagate requests unchanged, weaken configured permissions, or report unexecuted work as PASS. It may originate a new Depth-1 permission request only after independent re-evaluation and only when that operation is already Ask/allow under its own profile.

## 3. Repository composition model

Templates are composed from reusable components instead of duplicating a complete tree per language.

```text
components/
├── agent-core/
│   ├── opencode.json
│   ├── AGENTS.md
│   ├── Justfile
│   ├── .opencode/
│   │   ├── agents/
│   │   ├── commands/
│   │   └── skills/
│   └── .automation/
│       ├── VERSION
│       ├── UPSTREAM
│       ├── INIT.md
│       ├── policy.toml
│       ├── just/
│       ├── bin/
│       └── templates/
│
└── adapters/
    └── <adapter>/
        ├── flake.nix
        ├── just/project/
        ├── INIT.fragment.md
        └── adapter-specific CI fragments

templates/
└── <generated-template>/
```

Generated templates are build artifacts of the composition process and should not be manually edited when the same source belongs to Agent Core or an Adapter component.

## 4. Generated repository layout

A generated Agent-ready repository should converge on the following structure:

```text
repository/
├── opencode.json
├── AGENTS.md
├── Justfile
├── flake.nix
├── flake.lock
├── .opencode/
│   ├── agents/
│   ├── commands/
│   └── skills/
├── .automation/
│   ├── VERSION
│   ├── UPSTREAM
│   ├── INIT.md
│   ├── policy.toml
│   ├── just/
│   │   ├── agent.just
│   │   └── integrate.just
│   ├── bin/
│   └── templates/
├── just/
│   └── project/
│       ├── mod.just
│       ├── repository.just
│       └── adapter-specific modules
├── .github/
│   └── workflows/
└── .worktrees/
```

`.task-state/` exists only inside dedicated Task worktrees and is not part of the tracked template tree.

## 5. Ownership boundaries

| Area | Owner | May contain | Must not contain |
| --- | --- | --- | --- |
| `opencode.json` | Agent Core | repository-local permissions, model allocation, orchestration topology | project toolchain commands |
| `.opencode/**` | Agent Core | agents, skills, commands | language-specific build logic |
| `AGENTS.md` | Agent Core + generated adapter guidance | durable repository agent rules | mutable Task progress |
| `.automation/**` | Agent Core | lifecycle scripts, Task State template, integration gates | project-specific compiler/test implementation |
| `Justfile` | Agent Core | module routing only | large recipe bodies |
| `just/project/**` | Project Adapter + repository extension | build/test/lint/toolchain recipes | Task lifecycle, PR merge logic |
| `flake.nix` / `flake.lock` | Project Adapter / repository | reproducible tools and project dependencies | Agent lifecycle policy |
| `.task-state/**` | active Task Orchestrator | Task contract, Work Units, evidence, publication state | durable project configuration |
| `.github/workflows/**` | Agent Core + Project Adapter | CI gates and project checks | ad-hoc Task state |

## 6. Automation Core protection boundary

The following paths form the default Automation Core and are protected from ordinary implementation Tasks:

```text
opencode.json
AGENTS.md
Justfile
.opencode/**
.automation/**
.github/workflows/**
```

`flake.nix` and `flake.lock` are not globally immutable because dependency and environment Tasks may legitimately edit them. Their modification must be explicit in the Task scope.

Changes to the Automation Core require a dedicated Automation Maintenance Task and stronger review than ordinary implementation work.

## 7. Just module architecture

The top-level `Justfile` is a router, not a monolithic command file.

Conceptually:

```just
mod agent '.automation/just/agent.just'
mod integrate '.automation/just/integrate.just'
mod project 'just/project/mod.just'
mod? local 'just/local.just'
```

The namespaces have separate responsibilities:

- `agent::*`: Task lifecycle and publication API.
- `project::*`: stable project verification and build API.
- `integrate::*`: repository-wide PR integration API.
- `local::*`: optional developer-local commands; never part of the agent auto-allow contract.

Internal helper recipes should remain private and should not be treated as public API.

## 8. Stable Just API

### 8.1 Agent API

Every Agent-ready repository should expose the following logical operations:

```text
just agent::doctor
just agent::context
just agent::task-start <TASK-ID> <slug>
just agent::status <TASK-ID>
just agent::verify <TASK-ID>
just agent::commit <TASK-ID>
just agent::push <TASK-ID>
just agent::pr-create <TASK-ID>
just agent::pr-edit <TASK-ID>
just agent::pr-ready <TASK-ID>
just agent::cleanup <TASK-ID>
```

Semantics:

- `doctor`: validate Agent Core prerequisites without repairing them.
- `context`: resolve repository/worktree/branch/Task/adapter context in machine-readable form.
- `task-start`: create a dedicated branch/worktree and initialize Task State.
- `status`: report current Task state and Git relationship.
- `verify`: execute the stable project verification contract and record evidence.
- `commit`: validate scope and create Task-local commits.
- `push`: push only the Task branch through an explicit refspec.
- `pr-create`: create a Draft PR for the Task branch.
- `pr-edit`: update only approved PR metadata for the Task PR.
- `pr-ready`: mark the Task PR ready only after required gates pass.
- `cleanup`: remove Task-local resources after safe completion.

### 8.2 Project Adapter API

Every adapter must provide a stable compatibility layer:

```text
just project::doctor
just project::format-check
just project::lint
just project::test
just project::build
just project::check
```

Adapters may expose additional toolchain-specific submodules such as:

```text
project::cmake::*
project::cargo::*
project::nix::*
project::quality::*
project::repository::*
```

OpenCode permissions should normally target the stable top-level `project::*` contract, not every toolchain implementation detail.

### 8.3 Integration API

Repository-wide integration is exposed separately:

```text
just integrate::check <PR>
just integrate::merge <PR>
```

`integrate::check` is read-only or validation-only. `integrate::merge` performs the final integration boundary and must remain permission-gated.

## 9. OpenCode orchestration topology

The project-local OpenCode configuration uses `subagent_depth = 2`.

```text
Depth 0: Main Orchestrator (`build`)
│
├── Depth 1: Task Orchestrator A
│   ├── Depth 2: explore
│   ├── Depth 2: general
│   ├── Depth 2: verifier
│   └── Depth 2: specialist reviewer/investigator when required
│
└── Depth 1: Task Orchestrator B
    ├── Depth 2: explore
    ├── Depth 2: general
    └── Depth 2: verifier
```

The call graph is intentionally acyclic.

### 9.1 Allowed delegation graph

```text
build
├── task-orchestrator
├── architect
├── reviewer
├── security-reviewer
├── investigator
└── scout

task-orchestrator
├── explore
├── general
├── verifier
├── reviewer
├── investigator
├── security-reviewer
└── scout

leaf agents
└── no subagents
```

A Task Orchestrator may not call another Task Orchestrator. Leaf agents have `task: deny`.

Depth-2 leaf work is non-interactive. A leaf may return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, and `NEEDS_DECISION`; no leaf may originate direct permission requests outside its configured allowlist.

Depth-1 Task Orchestrator is the decision boundary for `NEEDS_APPROVAL` and `NEEDS_DECISION`, including independent re-checks for scope, authority, safety, least privilege, alternatives, and evidence.
It resolves `NEEDS_DECISION` from the Task Contract/evidence when possible. If human judgment remains necessary, it uses `question` directly from Depth 1 with options, tradeoffs, known facts, and a recommendation, then applies the response.

## 10. Agent responsibilities

### 10.1 Main Orchestrator

Owns repository-wide decisions:

- receives explicitly selected Tasks;
- resolves Task dependencies and likely integration overlap;
- creates one branch/worktree per Task;
- starts Task Orchestrators;
- limits Task-level parallelism;
- observes Draft PR results;
- determines integration order;
- validates CI/review/head SHA before merge;
- performs guarded merge and cleanup.

The Main Orchestrator does not silently select the next available Issue or Task.

### 10.2 Task Orchestrator

Owns one Task only:

- validates initialization and Task boundaries;
- establishes the Task Contract;
- decomposes work into bounded Work Units;
- coordinates and delegates only needed leaf work; avoids speculative or repeated delegation when one Work Unit can be reused;
- preserves a single-workflow focus by assigning each Work Unit once unless new evidence requires a reschedule;
- inspects actual diffs and command output;
- accepts or rejects returned Work Units and all leaf escalations on depth-1 authority.
  - re-validates scope, configured authority, least privilege, safety, alternatives, and evidence before deciding any `NEEDS_APPROVAL`/`NEEDS_DECISION`.
  - may only approve follow-on operations already in its own Task configuration.
  - on rejection, chooses a safe alternative or returns `BLOCKED` with evidence.
  - treats a user-rejected permission decision as final for the exact operation within that Task; records the permission result and never retries, rephrases, re-delegates, or substitutes an equivalent operation.
  - resolves `NEEDS_DECISION` from available evidence or asks the user directly from Depth 1 and continues from the answer.
- does not automatically relay or launder leaf escalation requests or weaken permissions to satisfy them. A new Depth-1 request requires independent justification and existing Ask/allow authority in the orchestrator profile.
- updates Task State;
- verifies and reviews the integrated Task;
- prepares commits and pull-request state;
- stops before merge.

### 10.3 Leaf agents

Leaf agents are execution specialists:

- `explore`: read-only code discovery and reference tracing;
- `general`: bounded implementation within exclusive edit scope;
- `verifier`: executable tests/lint/type-check/build verification;
- `reviewer`: correctness review through native read/search tools only; Bash remains fully denied;
- `investigator`: root-cause diagnosis;
- `security-reviewer`: security-boundary review;
- `scout`: external primary-source research.

Leaf agents never update Task State and never create another generation of subagents.
Leaf escalation protocol:

- report only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
- do not issue raw asks for repository permissions; any escalation is returned through the status channel.
- include evidence sufficient for the Task Orchestrator to independently validate scope, authority, safety, and alternatives.
- when consequential requirements are ambiguous, report `NEEDS_DECISION` with rationale rather than attempting speculative execution.

## 11. Task and Work Unit model

### 11.1 Task

A Task is a user-visible, independently reviewable deliverable.

Invariant:

```text
1 Task = 1 branch = 1 worktree = 1 Task State = 1 Task Orchestrator
```

Tasks may run concurrently only in separate worktrees.

### 11.2 Work Unit

A Work Unit is an internal bounded unit delegated by a Task Orchestrator. It is not a new project Task and does not receive its own worktree.

Every delegated Work Unit must specify:

- ID;
- objective;
- inputs;
- worktree;
- read scope;
- exclusive edit scope;
- deliverable;
- verification requirement;
- dependencies;
- prohibited changes;
- stop conditions.

Independent Work Units may run concurrently when edit scopes and stateful effects do not overlap.

## 12. Worktree contract

Dedicated worktrees use:

```text
.worktrees/<TASK-ID>-<slug>/
```

Task branch names contain the Task ID, for example:

```text
task/<TASK-ID>-<slug>
fix/<TASK-ID>-<slug>
```

The default branch is discovered from Git metadata and is never assumed to be `main` or `master`.

Before worktree creation, automation must validate:

- Task ID is explicit;
- base branch and base revision are resolvable;
- branch does not already conflict;
- path does not already exist;
- branch is not checked out by another worktree;
- existing user work is not overwritten.

The generated repository should ignore `/.worktrees/`.

## 13. Disposable Task State

Each Task worktree contains:

```text
.task-state/task.md
```

`.task-state/` is excluded through the Git common directory's `info/exclude`, not as durable project content.

Task State records at least:

- Task identity and source;
- branch/worktree/base revision;
- execution context;
- Purpose;
- Scope;
- Prohibited changes;
- Dependencies;
- Acceptance Criteria;
- Test plan;
- Stop conditions;
- current status;
- Work Units;
- changed files;
- commands and results;
- review evidence;
- commit / remote branch / PR / published head SHA;
- blockers and unverified requirements;
- follow-up Task candidates.

Task State is written only by the Task Orchestrator. Leaf agents return structured update proposals instead.

Task State is deleted with its worktree and must not be committed, pushed, or copied into a PR.

## 14. Task lifecycle

The baseline Task state machine is:

```text
initialized
  ↓
researching
  ↓
planning
  ↓
implementing
  ↓
verification-pending
  ↓
local-verified
  ↓
review-pending
  ↓
publication-ready
  ↓
draft-pr-created
  ↓
integration-pending
  ↓
merged
```

Exceptional terminal/interruption states:

```text
blocked
cancelled
```

A Task is never considered complete merely because all leaf agents returned successfully. Completion is determined from Acceptance Criteria and evidence.

## 15. Initialization contract

Initialization has two distinct meanings.

### 15.1 Bootstrap

Bootstrap occurs when a repository/template is first created. It may generate or configure tracked repository files such as `AGENTS.md`, adapter files, and initial project configuration.

Bootstrap is state-changing and is not part of ordinary session initialization.

### 15.2 Session `/init`

Session initialization is read-only.

The initialization contract is defined by:

```text
AGENTS.md
.automation/INIT.md
just agent::doctor
just agent::context
just project::doctor
```

Before planning, editing, delegation, or project command execution, a primary agent must:

1. read durable repository guidance;
2. validate Agent Core prerequisites;
3. resolve repository/worktree/branch/Task/adapter context;
4. validate the Project Adapter;
5. capture baseline HEAD and Git status;
6. confirm the Task Contract;
7. begin Work Unit decomposition only after the checks pass.

`/init` does not rewrite `AGENTS.md`, install packages, repair Automation Core, or begin implementation.

## 16. Permission model

Permissions are designed to minimize Ask frequency while preserving explicit remote/integration boundaries.

### 16.1 Auto-allowed operations

Expected auto-allowed categories:

- OpenCode native read/glob/grep/list/lsp;
- selected read-only Git and GitHub inspection;
- stable project verification/build recipes;
- Task-local commit recipe after validation;
- Task-local Draft PR create/edit/ready recipes after their gates pass.

Raw shell commands are not treated as safe merely because their common use is read-only. For example, `echo`, `cat`, `sed`, or `jq` can participate in file writes through shell syntax.

### 16.2 Ask operations

Default Ask boundaries:

- Task branch push;
- PR merge;
- Task/worktree cleanup that deletes state;
- `/tmp/opencode/**` external-directory access;
- unclassified Bash operations.

The Main OpenCode TUI is the user approval hub for permission requests generated by descendant sessions.

Native Depth-2 Ask propagation remains a non-gating upstream compatibility canary for `anomalyco/opencode#13715`.
The release gate for delegated work is the Depth-1 Task Orchestrator decision on `Leaf → Depth-1` escalations (`NEEDS_APPROVAL`/`NEEDS_DECISION`).

### 16.3 Denied operations

Default hard-deny boundaries include:

- force push;
- commit amend;
- rebase;
- destructive reset/clean;
- direct push to the default branch;
- Task Orchestrator merge;
- admin/bypass merge;
- privilege escalation;
- destructive store/filesystem operations.

Raw state-changing Git/GitHub commands should not be auto-allowed when an approved Just API exists.

## 17. Publication boundary

Task Orchestrators may advance a Task through publication preparation, but merge remains outside their authority.

Expected publication sequence:

```text
verify
→ commit
→ push (Ask)
→ create Draft PR
→ edit PR metadata as needed
→ mark ready when gates pass
→ stop at integration-pending
```

The publication API validates at least:

- Task/branch/worktree consistency;
- non-default branch;
- completed required Acceptance Criteria;
- required verification and review;
- no unresolved blockers;
- `.task-state/**` exclusion;
- no unauthorized Automation Core changes;
- explicit publish scope.

Push is restricted to the Task branch with an explicit refspec. Force push is never part of the ordinary Task API.

## 18. Integration boundary

Only the Main Orchestrator uses `integrate::*`.

Before merge, integration checks must validate:

- PR is open and targets the expected default branch;
- Task branch identity is valid;
- required CI passes;
- required review is complete;
- security review is complete when applicable;
- dependencies are already integrated;
- merge conflict status is acceptable;
- reviewed/verified head SHA still matches current PR head;
- Automation Core changes are expected when present.

Merge itself remains an Ask operation.

Administrative bypass is not part of the normal API.

## 19. Project Adapter contract

The Project Adapter is responsible for reproducible project-specific tooling while preserving the same agent-facing interface across languages.

Examples:

- Python adapter may implement `project::check` using Ruff, Mypy, Pytest, and uv.
- Rust adapter may implement it using rustfmt, Clippy, Cargo test, and Cargo build.
- Nix adapter may implement it using flake evaluation/check/build and Nix-specific linting.
- C++/CMake adapter may implement it using CMake, Ninja, CTest, clang-format, and clang-tidy.

Agent Core calls the stable API and does not infer these implementation details.

Adapters may define additional nested toolchain namespaces, but those are not automatically part of the OpenCode allowlist.

## 20. Repository extension and local modules

Project-specific operations that are not generic to the language/toolchain belong under:

```text
project::repository::*
```

Developer-machine-specific commands belong in an optional local module such as:

```text
local::*
```

Local commands are never automatically exposed to agents.

## 21. Model allocation contract

The intended initial model allocation is:

| Role | Model family | Responsibility |
| --- | --- | --- |
| Main Orchestrator / planning / architecture | GPT-5.6 Sol | decomposition, orchestration, integration decisions |
| Task Orchestrator | GPT-5.3 Codex Spark | fast bounded Task orchestration |
| general / explore / verifier / scout | GPT-5.6 Luna | implementation, discovery, verification, external research |
| reviewer / investigator / security-reviewer | GPT-5.6 Terra | analysis, diagnosis, review |

This is the final split: `task-orchestrator` is the only Spark primary, while all listed leaf execution/research roles use Luna-first allocation.
High-quality analysis roles remain as configured (reviewer/investigator/security-reviewer on Terra).

Exact provider model IDs are validated at implementation time. Missing model IDs must not be silently substituted with similar names.

## 22. Parallelism contract

Two separate kinds of parallelism are supported.

### 22.1 Task-level parallelism

The Main Orchestrator may run multiple Task Orchestrators in parallel when:

- Task dependencies permit it;
- likely edit regions do not create obvious integration hazards;
- shared lockfiles, schemas, generated outputs, databases, containers, ports, and other stateful resources are accounted for.

Each parallel Task uses its own worktree.

### 22.2 Work-Unit-level parallelism

A Task Orchestrator may run bounded leaf Work Units in parallel when:

- files do not overlap;
- shared manifests/lockfiles are not concurrently modified;
- one Work Unit does not depend on another's output;
- stateful external resources are isolated.

Parallelism is bounded rather than unlimited. Exact initial concurrency limits are implementation policy, not part of this architecture contract.

## 23. External resource isolation

Git worktrees isolate checked-out files and index state, but do not automatically isolate external resources.

Adapters and repository extensions must account for shared resources such as:

- TCP ports;
- Docker Compose project names;
- container names;
- temporary databases;
- Unix sockets;
- `/tmp` files;
- build output directories;
- coverage output;
- generated files;
- Nix result symlinks.

Where safe, Task ID should be used as a namespace. If a safe namespace cannot be derived from repository policy, automation stops instead of inventing one.

## 24. Configuration layering

Generated repositories own their development policy through repository-local configuration.

Global OpenCode configuration should be limited to user-level concerns such as provider configuration, credentials integration, TUI preferences, and conservative fallbacks.

Repository-local configuration owns:

- orchestration topology;
- model allocation;
- `subagent_depth`;
- Just API permissions;
- Task lifecycle;
- project-local commands and skills.

Repository-local policy is therefore part of the trusted repository surface and is included in the Automation Core protection boundary.

## 25. Template generation contract

The Templates repository should eventually generate concrete templates by composing:

```text
Agent Core + one Project Adapter + generated metadata
```

Composition must be deterministic and must not silently overwrite conflicting files.

Generated artifacts should carry enough metadata to identify their Agent Core version, upstream source, and selected adapter.

The generation mechanism and upgrade/versioning implementation are delegated to later issues.

## 26. Non-goals

This architecture does not require:

- an OpenCode process manager or daemon;
- automatic approval of Ask permissions;
- automatic Task selection;
- a permanent central Task Ledger;
- Task-Orchestrator-driven merge;
- automatic branch-protection bypass;
- all language adapters to be implemented at once;
- local Ollama models to be assigned to agents.

## 27. Migration guidance for durable escalation architecture (breaking behavior)

This contract changes leaf execution semantics and advances Agent Core from version 1 to version 2. Existing generated repositories must apply this as a dedicated Automation Maintenance upgrade rather than copying individual prompts or permission rules into an ordinary implementation Task:

1. Deploy the new Task Orchestrator and Task Orchestrator fallback prompts (including exact status contract and escalation re-check rules) to every generated repository path that carries these agents.
2. Update leaf agent instructions and any agent templates so Depth-2 units are non-interactive and return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
3. Add/validate tooling checks that treat any unrecognized leaf status as invalid evidence and block the Task.
4. Require depth-1 re-evaluation on `NEEDS_APPROVAL`/`NEEDS_DECISION`, including explicit checks for:
   - allowed scope and worktree
   - configured authority and prohibited changes
   - least-privilege and safety implications
   - alternative actions
   - current evidence quality.
5. Enforce that leaf-to-depth-1 escalation is a release gate; only completion evidence or explicit `BLOCKED` with rationale passes the boundary.
6. Preserve existing lifecycle invariants: initialization contract, fallback policy behavior, no merge from depth-1, no sibling worktree access.
7. Run `docs/opencode-depth2-ask-smoke.md` as an upstream compatibility canary, but treat it as non-gating for release.

## 28. Follow-up implementation mapping

This contract intentionally splits implementation across the Epic issues:

- #5: component + adapter template generation;
- #6: Agent Core Just modules and scripts;
- #7: repository-local OpenCode configuration and hierarchical agents;
- #8: worktree Task lifecycle and Task State;
- #9: `AGENTS.md` and initialization contract;
- #10: Python/Rust adapter migration;
- #11: Nix adapter;
- #12: C++/CMake adapter;
- #13: CI, negative tests, and Ask propagation smoke tests;
- #14: versioning, upgrades, and user documentation.

Later issues should treat the stable contracts in this document as authoritative unless an explicit architecture change updates this document first.
