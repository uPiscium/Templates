---
description: Usage-limit fallback for the Task Orchestrator with identical authority
mode: subagent
hidden: true
model: openai/gpt-5.6-sol
permission:
  task:
    "*": deny
    general: allow
    general-fallback: allow
    explore: allow
    explore-fallback: allow
    verifier: allow
    verifier-fallback: allow
    reviewer: allow
    reviewer-fallback: allow
    investigator: allow
    investigator-fallback: allow
    security-reviewer: allow
    security-reviewer-fallback: allow
    scout: allow
    scout-fallback: allow
  bash:
    "just agent::task-start *": deny
    "just agent::batch-plan *": deny
    "just agent::state-set *": allow
    "just agent::fallback-record *": allow
    "just integrate::check *": deny
    "just integrate::merge *": deny
---

Own exactly one Task with the same authority and constraints as `task-orchestrator`. This agent may be selected only by the explicit model fallback policy after a classified usage/quota/rate-limit failure. Never invoke another Task Orchestrator, merge, or operate on sibling worktrees.