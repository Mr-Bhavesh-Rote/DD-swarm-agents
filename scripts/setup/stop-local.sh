#!/usr/bin/env bash
# stop-local.sh — stop the locally-running services started by run-local.sh.
# Kills tracked PIDs, then frees the API/UI ports as a fallback (handles vite/uvicorn
# child processes). Does NOT stop docker DB/redis (use: docker compose down).
#
# Usage: scripts/setup/stop-local.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

kill_pidfile() {  # <pidfile> <name>
  local f="$1" name="$2" pid
  if [ -f "$f" ]; then
    pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
      ok "Stopped $name (pid $pid)."
    else
      warn "$name (pid $pid) was not running."
    fi
    rm -f "$f"
  else
    warn "No pid file for $name."
  fi
}

free_port() {  # <port> <name>
  local port="$1" name="$2" pids
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
      sleep 1
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      ok "Freed port ${port} ($name)."
    fi
  fi
}

info "Stopping local services ..."
kill_pidfile "$RUN_DIR/api.pid"      "API"
kill_pidfile "$RUN_DIR/worker.pid"   "Worker"
kill_pidfile "$RUN_DIR/frontend.pid" "Frontend"

# Fallback: clean up any leftover listeners (uvicorn --reload / vite child procs).
free_port "$API_PORT" "API"
free_port "$UI_PORT"  "Frontend"

echo
ok "stop-local complete."
info "To stop the database/redis too: docker compose -f docker-compose.yml down"
