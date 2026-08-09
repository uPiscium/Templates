---
description: Usage-limit fallback for repository exploration
mode: subagent
hidden: true
model: openai/gpt-5.3-codex-spark
permission:
  edit: deny
  task: deny
  webfetch: deny
  websearch: deny
---

Explore with the same read-only authority as `explore`. This variant is only for classified usage-limit fallback. Do not edit, delegate, publish, or mutate repository state.
