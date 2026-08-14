---
description: Clear an explicit Task recovery state
agent: build
---

Load the `initialize` skill and complete read-only checks. Then use the guarded recovery-clear API for the explicitly supplied Task/worktree context: `$ARGUMENTS`. Do not edit Task State directly or clear an unspecified recovery.
