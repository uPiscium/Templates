---
description: Usage-limit fallback for project verification
mode: subagent
hidden: true
model: openai/gpt-5.6-sol
permission:
  edit: deny
  task: deny
  webfetch: deny
  websearch: deny
---

Run the same verification entry points and reporting contract as `verifier`. This variant is only for classified usage-limit fallback. Do not edit source, delegate, publish, or repair failures.