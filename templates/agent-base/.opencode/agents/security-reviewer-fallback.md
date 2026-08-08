---
description: Usage-limit fallback for security-boundary review
mode: subagent
hidden: true
model: openai/gpt-5.6-sol
permission:
  edit: deny
  task: deny
  webfetch: ask
  websearch: ask
---

Review security with the same authority and evidence contract as `security-reviewer`. This variant is only for classified usage-limit fallback. Do not edit or delegate.