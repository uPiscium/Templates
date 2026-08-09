---
description: Usage-limit fallback for bounded implementation work
mode: subagent
hidden: true
model: openai/gpt-5.3-codex-spark
permission:
  task: deny
  bash:
    "just agent::task-start *": deny
    "just agent::commit *": deny
    "just agent::push *": deny
    "just agent::pr-create *": deny
    "just agent::pr-edit *": deny
    "just agent::pr-ready *": deny
    "just agent::cleanup *": deny
    "just agent::fallback-record *": deny
    "just integrate::check *": deny
    "just integrate::merge *": deny
---

Implement only the assigned Work Unit with the same scope and authority as `general`. This variant is only for classified usage-limit fallback. Do not update Task State, publish, integrate, clean up, or delegate.
