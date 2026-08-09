#!/usr/bin/env bash
set -euo pipefail

issue="${1:-issue-41}"
mode="${2:-child-stall}"
root="$(git rev-parse --show-toplevel)"
runner="$root/tests/runtime/runtime_smoke.py"
workspace="$root/.runtime-smoke/$issue"
repo="$workspace/smoke-repo"
logs="$workspace/logs"

if [[ ! -d "$repo" ]]; then
  echo "missing prepared runtime fixture: $repo" >&2
  echo "run: just runtime::prepare $issue" >&2
  exit 2
fi

opencode_bin="$(command -v opencode || true)"
if [[ -z "$opencode_bin" ]]; then
  echo "opencode is not available in PATH" >&2
  exit 2
fi

mkdir -p "$logs" "$workspace/evidence"
stamp="$(date +%Y%m%dT%H%M%S)"
log="$logs/opencode-${mode}-${stamp}.log"

python3 "$runner" snapshot-sessions --issue "$issue" --label "before-$mode-$stamp" >/dev/null

cd "$repo"
export OPENCODE_BIN="$opencode_bin"
export OPENCODE_DISABLE_AUTOUPDATE=1

echo "OpenCode runtime diagnostic"
echo "  issue: $issue"
echo "  mode:  $mode"
echo "  repo:  $repo"
echo "  log:   $log"
echo

set +e
nix develop --command bash -c '
  set -euo pipefail
  export VIRTUAL_ENV="$PWD/.venv"
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  export PIP_REQUIRE_VIRTUALENV=1
  export PATH="$PWD/.venv/bin:$PATH"
  exec "$OPENCODE_BIN" --print-logs --log-level DEBUG
' 2> >(tee "$log" >&2)
status=$?
set -e

python3 "$runner" snapshot-sessions --issue "$issue" --label "after-$mode-$stamp" >/dev/null || true

echo
echo "debug log: $log"
echo "report:    $workspace/reports/REPORT.md"
exit "$status"
