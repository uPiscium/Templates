---
description: Evaluate explicitly named Tasks for safe parallel execution
agent: build
---

Evaluate only the Task IDs supplied in `$ARGUMENTS` with `just agent::batch-plan ...`.

Do not discover or auto-select additional Tasks. If dependencies, declared scope, coordination surfaces, or external resources conflict, report the conflicting pairs and serialize them. Launch Task Orchestrators only for Tasks that are explicitly requested and assessed as parallel-safe.
