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

After instantiating a generated non-Python language/toolchain template, run the one-time project bootstrap before the first validation or development session:

```sh
nix develop --command just project::bootstrap
```

For the Python template, prevent the outer Nix invocation from writing `flake.lock`; the Python Adapter bootstrap owns explicit materialization of missing `flake.lock` and `uv.lock` files:

```sh
nix develop --no-write-lock-file --command just project::bootstrap
```

The optional explicit project name can be supplied when the directory name is not the desired project name. Preserve the same Python-specific flag when using it:

```sh
nix develop --command just project::bootstrap my-project
nix develop --no-write-lock-file --command just project::bootstrap my-project
```

`project::bootstrap` is the explicit state-changing setup step. It resolves generated project-name placeholders and is idempotent. Python uses `--no-write-lock-file` so outer `nix develop` may resolve inputs but cannot take ownership of lockfile mutation; the Adapter then creates missing repository-owned `flake.lock` and `uv.lock` files without refreshing existing lockfiles. It does not commit, push, merge, or change GitHub repository settings. Include bootstrap output in the initial commit, then use `nix develop --no-update-lock-file --command ...` and the corresponding Just checks for normal read-only validation; Python verification uses `uv run --locked` and does not repair missing or stale dependency identity.

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

For bootstrap, adoption, or upgrade diagnostics that need only runtime readiness, run `just agent::preflight` from the root of an installed Agent Core. It checks required tools/files, Agent Core version, and a non-empty Adapter marker without resolving or relaxing branch/Task identity. Preflight is read-only, but `preflight = PASS` does not mean the checkout is a valid Agent session: `/init`, `agent::doctor`, and `agent::context` remain strict and may intentionally block a non-default branch without registered Task State.

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

Generated Agent-ready repositories include a repository-local `plan` agent for read-only planning. It is primary, uses `openai/gpt-5.6-sol`, denies `edit` and `bash`, allows `question` for consequential requirement clarification, and may delegate only to read-only inspection leaves: `explore`, `architect`, `reviewer`, and `security-reviewer`.

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

Each role uses one fixed GPT-5.6 model. Agent Core does not substitute models or retry an objective under another model. If the configured provider/model is unavailable, preserve Task and Work Unit evidence, report the exact failure, and return `BLOCKED`.

Task Orchestrators autonomously drive the persisted Work Unit loop through the
guarded `work-unit-next`, `work-unit-create`, and `work-unit-dispatch-check`
APIs. These Task-local APIs are globally denied and available only to the Task
Orchestrator. Every dispatch carries an exact role and objective; only
`COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, and `NEEDS_DECISION` are canonical
leaf statuses. Failed review, security, verifier, or check evidence requires
a fresh corrective Work Unit; needs-* statuses stop for human handling.

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

The upgrade system supports versioned removal migrations. Removals name explicit, exact Agent Core-managed paths; repository-owned and protected paths cannot be removed. Migration preconditions are checked before any mutation, and `.automation/VERSION` advances only after all deletion, creation, replacement, and merge actions succeed.

Upgrade does not commit, push, or merge. Before publication, inspect the diff and run at minimum:

```sh
git diff --check
just agent::doctor
just project::check
```

Then require the repository CI/smoke suite. Automation Core changes must be reviewed as a dedicated maintenance change.

## Template development

### OpenCodePolicy dependency

The Templates source repository pins [`upiscium/OpenCodePolicy`](https://github.com/upiscium/OpenCodePolicy) through the root `flake.lock` and audits this checkout against the explicit `agent-core` profile. OpenCodePolicy owns the shared policy and compatibility contract; Templates remains the Agent Core implementation and distribution owner.

The dependency is a Templates development and validation gate only. It is not rendered into generated templates, does not change `.automation/UPSTREAM`, and does not generate OpenCode configuration.

Policy revisions advance only through an explicit dependency update. The recommended path is **GitHub Actions → Update OpenCodePolicy → Branch = main → Run workflow**, or:

```sh
gh workflow run update-opencode-policy.yml \
  --repo upiscium/Templates \
  --ref main
```

This workflow runs only through an explicit `workflow_dispatch`; it has no schedule. It validates lock hygiene, Templates contracts, generated drift, distribution metadata, and the strict `agent-core` profile before creating a Draft pull request containing only `flake.lock`.

For a local update:

```sh
nix flake update opencodePolicy
nix flake check --no-update-lock-file
python3 -m unittest discover -s tests -v
just template::check
```

Review the resulting `flake.lock` diff and policy audit before merging. OpenCodePolicy `main` moving does not change Templates until this lockfile is deliberately updated. Generated templates do not receive the OpenCodePolicy input and are not regenerated by this dependency update. The resulting Draft pull request runs normal Template CI as a second gate, including the generated-template runtime smoke matrix.

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

The default Main agent orchestrates Tasks; Task Orchestrators own one Task; leaf agents perform bounded work and cannot re-delegate. Configured role models are fixed and fail closed when unavailable. Depth-2 Ask behavior has a separate reproducible manual smoke procedure under `docs/opencode-depth2-ask-smoke.md` and is not represented as PASS until executed.
