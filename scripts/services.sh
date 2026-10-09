#!/usr/bin/env bash
# Start, stop, or list the harness services. Logs go to .logs/, PIDs to .pids/.
#   scripts/services.sh up | down | status
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/.logs"; PIDS="$ROOT/.pids"
mkdir -p "$LOGS" "$PIDS"

# name|directory|port|command
SERVICES=(
  "mcp_server|mcp_server|8001|uv run python server.py"
  "child_langgraph|.|2024|uv run --project child_langgraph langgraph dev --port 2024 --no-browser --no-reload --allow-blocking"
  "adk_service|adk_service|8002|uv run python app.py"
  "adk_service_override|adk_service|8004|env PORT=8004 ADK_PROJECT=__ADK_OVERRIDE__ uv run python app.py"
  "strands_service|strands_service|8003|uv run python app.py"
  "mda_agent|mda_agent|2025|uv run mda dev --port 2025 --no-browser --no-reload"
  "mda_agent_patched|mda_agent|2026|bash ../scripts/run_mda_patched.sh"
)

adk_override_project() {
  (cd "$ROOT" && uv run --quiet python -c 'from harness import config; print(config.ADK_OVERRIDE_PROJECT)')
}

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

up() {
  local override; override="$(adk_override_project)"
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name dir port cmd <<<"$entry"
    if listening "$port"; then echo "  $name: port $port already in use, skipping"; continue; fi
    cmd="${cmd/__ADK_OVERRIDE__/$override}"
    # exec makes the recorded PID the service itself, and the redirects keep the
    # background job off the caller's stdout so `services.sh up | ...` returns.
    (cd "$ROOT/$dir" && exec nohup $cmd) </dev/null >"$LOGS/$name.log" 2>&1 &
    echo $! >"$PIDS/$name.pid"
    echo "  $name: starting on :$port (log .logs/$name.log)"
  done
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name dir port cmd <<<"$entry"
    for _ in $(seq 1 60); do listening "$port" && break; sleep 1; done
    listening "$port" && echo "  $name: up" || { echo "  $name: FAILED to start, see .logs/$name.log"; exit 1; }
  done
}

down() {
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name dir port cmd <<<"$entry"
    pidfile="$PIDS/$name.pid"
    [ -e "$pidfile" ] || continue  # only stop what this script started
    pid="$(cat "$pidfile")"
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    # uv and langgraph dev spawn grandchildren; stop whatever still holds our port.
    sleep 1
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | xargs kill -TERM 2>/dev/null || true
    rm -f "$pidfile"
    echo "  stopped $name"
  done
}

status() {
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name dir port cmd <<<"$entry"
    listening "$port" && echo "  $name :$port up" || echo "  $name :$port down"
  done
}

"${1:-status}"
