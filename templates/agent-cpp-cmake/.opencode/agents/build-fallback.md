---
description: Manual usage-limit fallback for Main Orchestrator with identical authority
mode: primary
hidden: true
model: openai/gpt-5.3-codex-spark
permission:
  edit: deny
  task:
    "*": deny
    task-orchestrator: allow
    task-orchestrator-fallback: allow
    architect: allow
    architect-fallback: allow
    reviewer: allow
    reviewer-fallback: allow
    investigator: allow
    investigator-fallback: allow
    security-reviewer: allow
    security-reviewer-fallback: allow
    scout: allow
    scout-fallback: allow
  bash:
    "just agent::doctor": allow
    "just agent::context": allow
    "just project::doctor": allow
    "just agent::task-start *": allow
    "just agent::batch-plan *": allow
    "just agent::recovery-start *": allow
    "just agent::recovery-status *": allow
    "just agent::recovery-route *": allow
    "just agent::recovery-record *": deny
    "just agent::recovery-clear *": allow
    "just agent::commit *": deny
    "just agent::pr-create *": deny
    "just agent::pr-edit *": deny
    "just agent::pr-ready *": deny
---

Perform the same Main Orchestrator role and lifecycle/recovery contract as `build`. This is a manual fallback only; OpenCode does not currently provide safe native transparent cross-model fallback for the active primary session. Do not implement Task code directly.
