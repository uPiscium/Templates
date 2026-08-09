#!/usr/bin/env bash
set -euo pipefail

issue="${1:-issue-41}"
root="$(git rev-parse --show-toplevel)"
workspace="$root/.runtime-smoke/$issue"
repo="$workspace/smoke-repo"
task_repo="$repo/.worktrees/SMOKE-CONTROL-ask-free-control"
logs="$workspace/logs"
timeout_seconds="${RUNTIME_TIMEOUT_SECONDS:-180}"
diagnostic_prompt='Diagnostic control only: run git status --short exactly once, do not edit files, and report the result.'

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
server_log="$logs/direct-leaf-${stamp}.serve.log"

cd "$task_repo"
export OPENCODE_BIN="$opencode_bin"
export OPENCODE_DISABLE_AUTOUPDATE=1
export RUNTIME_TIMEOUT_SECONDS="$timeout_seconds"
export RUNTIME_DIAGNOSTIC_PROMPT="$diagnostic_prompt"
export RUNTIME_SERVE_LOG="$server_log"

set +e
timeout --kill-after=20s "${RUNTIME_TIMEOUT_SECONDS}s" nix develop --command bash -c '
  set -euo pipefail

  export VIRTUAL_ENV="$PWD/.venv"
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  export PIP_REQUIRE_VIRTUALENV=1
  export PATH="$PWD/.venv/bin:$PATH"
  export OPENCODE_SERVE_LOG="$RUNTIME_SERVE_LOG"

  port="$(python3 -c '\''import socket; sock = socket.socket(); sock.bind(("127.0.0.1", 0)); print(sock.getsockname()[1]); sock.close()'\'')"
  export OPENCODE_BASE_URL="http://127.0.0.1:${port}"

  cleanup() {
    if [[ -n "${server_pid:-}" ]] && kill -0 "$server_pid" 2>/dev/null; then
      kill "$server_pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
          break
        fi
        sleep 0.25
      done
      if kill -0 "$server_pid" 2>/dev/null; then
        kill -KILL "$server_pid" 2>/dev/null || true
      fi
      wait "$server_pid" 2>/dev/null || true
    fi
    if [[ -f "$OPENCODE_SERVE_LOG" ]]; then
      cat "$OPENCODE_SERVE_LOG" >&2
    fi
  }
  trap cleanup EXIT

  "$OPENCODE_BIN" serve --print-logs --log-level DEBUG --hostname 127.0.0.1 --port "$port" >"$OPENCODE_SERVE_LOG" 2>&1 &
  server_pid=$!

  python3 - "${OPENCODE_BASE_URL}" "$RUNTIME_DIAGNOSTIC_PROMPT" <<'PY'
import json
import subprocess
import sys
import time
import urllib.request


base = sys.argv[1]
prompt = sys.argv[2]


def request(method: str, path: str, body=None):
    data = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        data = payload
    request_obj = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request_obj, timeout=30) as response:
        payload = response.read().decode("utf-8")
        return response.status, (json.loads(payload) if payload else None)


def status(message: str) -> None:
    print(json.dumps({"type": "status", "message": message}))


for _ in range(120):
    try:
        _status, health = request("GET", "/global/health")
        if health.get("healthy") is True:
            break
    except Exception:
        time.sleep(0.25)
else:
    raise RuntimeError("opencode local API did not start")

status("creating direct control session")
_status, session = request("POST", "/session", {"title": "Direct leaf control"})
session_id = session.get("id")
if not session_id:
    raise RuntimeError("session creation response did not include a session id")

status("sending diagnostic prompt")
_status, response = request(
    "POST",
    f"/session/{session_id}/message",
    {
        "agent": "general",
        "parts": [{"type": "text", "text": prompt}],
    },
)

info = response.get("info", {})
actual_agent = info.get("agent")
if actual_agent != "general":
    raise RuntimeError(f"direct control used unexpected agent: {actual_agent!r}")

status("collecting message evidence")
_status, messages = request("GET", f"/session/{session_id}/message")
tool_parts = [
    part
    for message in messages
    for part in message.get("parts", [])
    if part.get("type") == "tool"
]
if len(tool_parts) != 1:
    raise RuntimeError("general requested an unexpected additional tool action")
status_parts = [
    part
    for part in tool_parts
    if part.get("tool") == "bash"
    and part.get("state", {}).get("input", {}).get("command") == "git status --short"
]
if len(status_parts) != 1:
    raise RuntimeError("general did not request git status --short exactly once")
if status_parts[0].get("state", {}).get("status") != "completed":
    raise RuntimeError("git status --short did not complete")
worktree_status = subprocess.run(
    ["git", "status", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if worktree_status:
    raise RuntimeError(f"direct control modified the worktree: {worktree_status}")

print(
    json.dumps(
        {
            "type": "diagnostic-result",
            "sessionID": session_id,
            "agent": "general",
            "response": response,
            "messages": messages,
        }
    )
)
PY
' >"$events" 2> >(tee "$debug" >&2)
status=$?
set -e

echo "events: $events"
echo "debug:  $debug"
if [[ "$status" -eq 124 ]]; then
  echo "direct leaf control timed out after ${timeout_seconds}s" >&2
fi
exit "$status"
