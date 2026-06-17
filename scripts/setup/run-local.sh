#!/usr/bin/env bash
# run-local.sh — run both projects locally, standalone, in the background.
#   Backend API   : uvicorn app.main:app  (:8000, --reload in dev)
#   Backend worker: python worker.py      (RQ background runner)
#   Frontend      : vite dev server        (:5173)
#
# PIDs are written to .local-run/ ; logs to .local-run/logs/. Stop with stop-local.sh.
# Usage: scripts/setup/run-local.sh [dev|prod]   (default: dev)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

PROFILE="${1:-dev}"
BE_ENV="$(backend_env_file "$PROFILE")"
FE_ENV="$(frontend_env_file "$PROFILE")"

info "Profile: $PROFILE"
[ -d "$BACKEND/.venv" ]      || die "Backend .venv missing — run scripts/setup/setup-all.sh"
[ -d "$FRONTEND/node_modules" ] || die "Frontend node_modules missing — run scripts/setup/setup-all.sh"
[ -f "$BE_ENV" ]            || die "Missing $(basename "$BE_ENV") — run scripts/setup/setup-fresh.sh"
[ -f "$FE_ENV" ]           || die "Missing $(basename "$FE_ENV") — run scripts/setup/setup-fresh.sh"

# Infra reachability (started by setup-fresh.sh).
port_open 127.0.0.1 "$DB_PORT"    || warn "PostgreSQL not reachable on :${DB_PORT} — run setup-fresh.sh (runs may fail to persist)."
port_open 127.0.0.1 "$REDIS_PORT" || warn "Redis not reachable on :${REDIS_PORT} — run setup-fresh.sh (jobs/SSE need it)."

is_running() { local f="$1"; [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; }
start_guard() { local f="$1" name="$2"; if is_running "$f"; then warn "$name already running (pid $(cat "$f")). Run stop-local.sh first."; exit 1; fi; }

workers_running() {
  [ -f "$RUN_DIR/worker.pids" ] || return 1
  local p
  while read -r p; do
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && return 0
  done < "$RUN_DIR/worker.pids"
  return 1
}

start_guard "$RUN_DIR/api.pid" "API"
start_guard "$RUN_DIR/frontend.pid" "Frontend"
if workers_running; then
  warn "Worker(s) already running (pids: $(tr '\n' ' ' < "$RUN_DIR/worker.pids")). Run stop-local.sh first."
  exit 1
fi

# ------------------------------------------------------------------- backend api+worker
RELOAD=""; [ "$PROFILE" = "dev" ] && RELOAD="--reload"
info "Starting backend API on :${API_PORT} ..."
(
  cd "$BACKEND"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  set -a; # shellcheck disable=SC1090
  source "$BE_ENV"; set +a
  # shellcheck disable=SC2086
  nohup uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT" $RELOAD \
    > "$LOG_DIR/api.log" 2>&1 &
  echo $! > "$RUN_DIR/api.pid"

  info "Starting ${WORKER_CONCURRENCY} background worker(s) (= max parallel runs) ..."
  : > "$RUN_DIR/worker.pids"
  for i in $(seq 1 "$WORKER_CONCURRENCY"); do
    nohup python worker.py > "$LOG_DIR/worker.$i.log" 2>&1 &
    echo $! >> "$RUN_DIR/worker.pids"
  done
)
ok "API pid $(cat "$RUN_DIR/api.pid") · workers $(tr '\n' ' ' < "$RUN_DIR/worker.pids")"

# -------------------------------------------------------------------------- frontend
info "Starting frontend dev server on :${UI_PORT} ..."
(
  cd "$FRONTEND"
  set -a; # shellcheck disable=SC1090
  source "$FE_ENV"; set +a
  nohup npm run dev -- --host --port "$UI_PORT" > "$LOG_DIR/frontend.log" 2>&1 &
  echo $! > "$RUN_DIR/frontend.pid"
)
ok "Frontend pid $(cat "$RUN_DIR/frontend.pid")"

echo
ok "All services starting."
info "API:      http://localhost:${API_PORT}  (docs: /docs)"
info "Frontend: http://localhost:${UI_PORT}"
info "Workers:  ${WORKER_CONCURRENCY} (max parallel runs) — logs: $LOG_DIR/worker.*.log"
info "Logs:     $LOG_DIR/{api,frontend}.log"
info "Verify:   scripts/setup/verify-all.sh $PROFILE"
info "Stop:     scripts/setup/stop-local.sh"
