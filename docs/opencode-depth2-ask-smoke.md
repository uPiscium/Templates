# OpenCode Depth-2 Ask Smoke Test

Related: #7

## Status

UNVERIFIED in the implementation environment. The repository configuration is present, but this environment does not provide a runnable OpenCode TUI session with the configured models and approval UI.

Do not treat this document as a PASS result until the procedure below is executed on a generated template.

## Purpose

Verify that a permission request originating from a Depth-2 leaf agent can be observed and handled from the single Main OpenCode TUI session without weakening repository permissions.

## Preconditions

- Generate or enter an Agent-ready repository containing the #7 configuration.
- `opencode` is installed and authenticated for the exact configured model IDs.
- `just`, `git`, `gh`, and the base Project Adapter are available.
- The current repository has a disposable test Task/worktree.

## Procedure

1. Start one OpenCode TUI in the repository Main worktree.
2. Confirm the resolved config reports `subagent_depth = 2`.
3. From `build`, launch one `task-orchestrator` for the disposable Task.
4. From that Task Orchestrator, launch a Depth-2 leaf agent.
5. In the leaf agent, request a harmless command that matches the default Bash `ask` rule but no explicit allow/deny rule.
6. Verify whether the approval request is surfaced in the Main TUI.
7. Approve it once and confirm only the requested command proceeds.
8. Repeat with a second harmless Ask and reject it; confirm rejection propagates to the leaf without weakening permissions.
9. Trigger `just agent::push <TASK-ID>` from the Task Orchestrator and confirm it requires Ask.
10. Confirm raw `git push ...` is denied rather than offered as an alternate approval path.
11. Confirm `just integrate::merge <PR>` is unavailable/denied from the Task Orchestrator and remains an Ask operation for the Main Orchestrator.
12. Confirm access to `/tmp/opencode/**` asks and another external directory is denied.

## Result record

Record:

- OpenCode version
- resolved configured model IDs
- Main session identifier, if exposed
- Task Orchestrator session identifier, if exposed
- leaf session identifier, if exposed
- Depth-2 Ask visible from Main TUI: PASS / FAIL / INCOMPLETE
- approval propagation: PASS / FAIL / INCOMPLETE
- rejection propagation: PASS / FAIL / INCOMPLETE
- Task push Ask: PASS / FAIL / INCOMPLETE
- raw push deny: PASS / FAIL / INCOMPLETE
- Task merge deny: PASS / FAIL / INCOMPLETE
- Main merge Ask: PASS / FAIL / INCOMPLETE
- `/tmp/opencode/**` Ask: PASS / FAIL / INCOMPLETE
- other external path deny: PASS / FAIL / INCOMPLETE

If the Main TUI cannot directly surface descendant Ask requests but navigating to the child session permits approval, record that behavior explicitly rather than treating it as equivalent to centralized approval.
