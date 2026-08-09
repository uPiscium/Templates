#!/usr/bin/env bash
set -euo pipefail

issue="${1:-issue-41}"
root="$(git rev-parse --show-toplevel)"
workspace="$root/.runtime-smoke/$issue"
repo="$workspace/smoke-repo"
task_repo="$repo/.worktrees/SMOKE-CONTROL-ask-free-control"
logs="$workspace/logs"
timeout_seconds="${RUNTIME_TIMEOUT_SECONDS:-180}"

if [[ ! -d "$task_repo" ]]; then
  echo "missing SMOKE-CONTROL worktree: $task_repo" >&2
  echo "run: just runtime::prepare $issue" >&2
  exit 2
fi

opencode_bin="$(command -v opencode || true)"
if [[ -z "$opencode_bin" ]]; then
  echo "opencode is not available in PATH" >&2
  exit 2
fi

mkdir -p "$logs"
stamp="$(date +%Y%m%dT%H%M%S)"
events="$logs/direct-leaf-${stamp}.jsonl"
debug="$logs/direct-leaf-${stamp}.debug.log"

cd "$task_repo"
export OPENCODE_BIN="$opencode_bin"
export OPENCODE_DISABLE_AUTOUPDATE=1
export RUNTIME_TIMEOUT_SECONDS="$timeout_seconds"

set +e
nix develop --command bash -c '
  set -euo pipefail
  export VIRTUAL_ENV="$PWD/.venv"
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  export PIP_REQUIRE_VIRTUALENV=1
  export PATH="$PWD/.venv/bin:$PATH"
  timeout "${RUNTIME_TIMEOUT_SECONDS}s" "$OPENCODE_BIN" --print-logs --log-level DEBUG run \
    --agent general \
    --format json \
    "Diagnostic control only: run git status --short exactly once, do not edit files, and report the result."
' >"$events" 2> >(tee "$debug" >&2)
status=$?
set -e

echo "events: $events"
echo "debug:  $debug"
if [[ "$status" -eq 124 ]]; then
  echo "direct leaf control timed out after ${timeout_seconds}s" >&2
fi
exit "$status"
