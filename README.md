# Templates

Nix flake templates for reproducible, Agent-ready development environments.

## Start a new repository

There are two new-repository distribution channels. Use a GitHub Template Repository when creating the repository on GitHub, or use the Nix flake template when initializing a local directory.

GitHub Template Repository targets:

```text
agent-python     -> upiscium/Template-Agent-Python
agent-rust       -> upiscium/Template-Agent-Rust
agent-nix        -> upiscium/Template-Agent-Nix
agent-cpp-cmake  -> upiscium/Template-Agent-Cpp-CMake
```

The GitHub distribution repositories are generated artifacts synchronized from this repository; they are not independent sources of truth. See `docs/github-template-distribution.md` for setup, publishing, and ownership details.

Local Nix templates remain available:

```sh
nix flake init -t github:upiscium/Templates#agent-base
nix flake init -t github:upiscium/Templates#agent-python
nix flake init -t github:upiscium/Templates#agent-rust
nix flake init -t github:upiscium/Templates#agent-nix
nix flake init -t github:upiscium/Templates#agent-cpp-cmake
```

Compatibility aliases remain available:

```text
python -> agent-python
rust   -> agent-rust
```

Every Agent-ready repository contains the shared Agent Core plus exactly one Project Adapter. Adapter-less repositories are not supported; unknown projects use `base` as the minimum contract.

After instantiating a generated language/toolchain template, run the one-time project bootstrap before the first validation or development session:

```sh
nix develop --command just project::bootstrap
```

The optional explicit project name can be supplied when the directory name is not the desired project name:

```sh
nix develop --command just project::bootstrap my-project
```

`project::bootstrap` is the explicit state-changing setup step. It resolves generated project-name placeholders and is idempotent. It does not commit, push, merge, or change GitHub repository settings. After bootstrap, initialize Git as needed and use `/init` or the corresponding Just checks for normal read-only session validation.

## Repository policy

Generated Agent-ready repositories declare a GitHub repository policy in `.automation/repository-policy.json`. The policy expects `main` as the default branch and protects the default branch with a repository ruleset: every change must arrive through a pull request, approving reviews are optional (`0` required), deletion and force pushes are blocked, and no bypass actor is configured.

Repository policy is deliberately separate from project bootstrap and `/init`. Inspect the current GitHub repository read-only with:

```sh
just repository::policy-check
```

Apply the declared policy explicitly with:

```sh
just repository::policy-apply
```

`repository::policy-apply` acts only on the GitHub repository resolved from the current checkout, requires GitHub Administration write permission, and is an Ask operation in OpenCode. If `main` does not exist, it refuses to invent or rename a branch; establish `main` explicitly first. Policy mutation is also refused from Task worktrees. See `.automation/REPOSITORY_POLICY.md` in generated repositories for the complete contract.

## Repository layers

```text
Agent Core
  .automation/**, .opencode/**, AGENTS.md, root Justfile, opencode.json

Project Adapter
  just/project/**, language/toolchain manifests, INIT.fragment.md, ADAPTER

Repository extension
  just/project/repository.just and repository-owned build/configuration files

Local extension
  just/local.just (optional, repository-specific convenience API)
```

Generated files under `templates/<name>/` are artifacts. Edit `components/agent-core/` or `components/adapters/<adapter>/` and regenerate instead.

## Initialization

Bootstrap, GitHub repository policy setup, and session initialization are intentionally separate:

```text
nix flake init ...
  -> just project::bootstrap          # one-time, state-changing project setup
  -> just repository::policy-apply    # optional explicit GitHub repository setup
  -> /init                            # every-session, read-only validation
```

`/init` is read-only. It validates Agent Core version, Adapter identity, branch/worktree/Task State, tools, project doctor, HEAD, and Git status. It never bootstraps, repairs, installs packages, changes Task State, rewrites `AGENTS.md`, or mutates GitHub repository settings.

Existing-repository adoption and Agent Core upgrade are separate mutating workflows from generated-project bootstrap.

## Existing repositories

Plan first:

```sh
cd /path/to/Templates
nix develop --command just template::adopt-plan /path/to/repository
```

Apply with an explicit Adapter when appropriate:

```sh
cd /path/to/Templates
nix develop --command just template::adopt-apply /path/to/repository base
nix develop --command just template::adopt-apply /path/to/repository python
```

Auto-detection prefers CMake/Python/Rust markers; a standalone `flake.nix` selects Nix; unknown or ambiguous repositories fall back to `base`.

Migrate a base-adopted repository by inspecting the read-only migration plan before changing Adapter-owned paths:

```sh
cd /path/to/Templates
nix develop --command just template::adapter-migrate-plan /path/to/repository python
```

Adoption/migration never commits, pushes, merges, stashes, or resets the target repository.


## Planning Agent

Generated Agent-ready repositories include a repository-local `plan` agent for read-only planning. It is primary, uses `openai/gpt-5.6-sol`, denies `edit` and `bash`, allows `question` for consequential requirement clarification, and may delegate only to read-only inspection leaves: `explore`, `architect`, `reviewer`, and `security-reviewer` plus their configured fallbacks.

`plan` never starts the Task lifecycle, edits Task State, runs executable doctor/check commands, or reports unexecuted verification as PASS. It reads `AGENTS.md`, `.automation/INIT.md`, adapter initialization guidance, and optional Task State, then returns confirmed facts, assumptions, open decisions, a bounded implementation plan, `execution_prerequisites`, and `verification_handoff` entries for an execution-capable workflow.

## Task and worktree lifecycle

Main schedules Tasks. Each Task owns one branch, one repo-local worktree under `.worktrees/`, one disposable `.task-state/task.md`, and one Task Orchestrator. Leaf agents cannot delegate or mutate Task State.

Typical flow:

```text
Main
  -> task-start
  -> Task Orchestrator implements/verifies
  -> guarded commit
  -> Ask: push
  -> Draft PR
  -> integration check
  -> Ask: merge (Main only)
  -> cleanup
```

Raw Git/GitHub writes are denied. Stable Just APIs provide the guarded write path. Push, merge, cleanup, unknown Bash, and designated external paths require Ask.

Model fallback is policy/classification based. Automatic same-turn cross-model failover is not available in OpenCode; prompt retry is best-effort. After a failure, use the supported explicit `/task-recover TASK FAMILY` workflow, which routes directly to the configured variant while preserving Task/worktree identity and runs to integration-pending. The contract harness is deterministic configuration coverage, not genuine runtime verification; native session-preserving migration remains future work.

## Project Adapter API

Generated language/toolchain Adapters expose the one-time bootstrap API plus the stable validation/build API:

```text
just project::bootstrap [name]
just project::doctor
just project::format-check
just project::lint
just project::test
just project::build
just project::check
```

Adapters may expose additional guarded APIs such as `project::eval` or `project::configure`, but broad raw tool commands are not automatically allowed.

To add an Adapter, create `components/adapters/<id>/` with `.automation/ADAPTER`, `.automation/INIT.fragment.md`, `just/project/mod.just`, adoption policy, required project files, and a manifest entry. Add source/generated parity tests and generated-template CI smoke coverage.

## Agent Core version and upstream

Generated repositories contain:

```text
.automation/VERSION
.automation/UPSTREAM
```

Inspect them through:

```sh
just automation::version
```

`UPSTREAM` records the canonical Templates repository/ref/component. Breaking Agent Core changes require a VERSION change and migration notes; compatible implementation/documentation changes may remain within the current version.

## Read-only update check

The repository never fetches or executes upstream code automatically. Check against a trusted local Templates checkout:

```sh
just automation::check-update /path/to/Templates
```

This reports current/upstream versions and the ownership boundaries without mutating the repository. Agent Core upgrades must never be silently mixed into an ordinary Task.

## Agent Core upgrade

Use a dedicated Task worktree. The upgrade command is an Ask operation and additionally requires an explicit maintenance marker:

```sh
export AUTOMATION_MAINTENANCE=1
just automation::upgrade /path/to/Templates
```

It refuses the default branch and repositories without Task State. It materializes only Agent Core-owned paths and preserves Adapter-owned `.automation/ADAPTER`, `.automation/INIT.fragment.md`, `.automation/adoption.toml`, `just/project/**`, local modules, and repository CI.

Upgrade does not commit, push, or merge. Before publication, inspect the diff and run at minimum:

```sh
git diff --check
just agent::doctor
just project::check
```

Then require the repository CI/smoke suite. Automation Core changes must be reviewed as a dedicated maintenance change.

## Template development

```sh
just template::render agent-base
just template::render agent-python
just template::render agent-rust
just template::render agent-nix
just template::render agent-cpp-cmake
just template::render-all
just template::check
just template::distribution-verify
```

`template::check` detects generated drift, path collisions, dotfile/mode drift, and unregistered generated directories. `template::distribution-verify` validates the fixed GitHub distribution allowlist; publication itself is CI-only after a successful Template CI run on current `main`.

## OpenCode hierarchy

The default Main agent orchestrates Tasks; Task Orchestrators own one Task; leaf agents perform bounded work and cannot re-delegate. Model fallback is role-scoped and only automatic for classified usage/quota/rate-limit failures. Depth-2 Ask behavior has a separate reproducible manual smoke procedure under `docs/opencode-depth2-ask-smoke.md` and is not represented as PASS until executed.
