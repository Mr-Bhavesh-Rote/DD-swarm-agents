#!/usr/bin/env bash
# verify-all.sh — verify the full installation: backend, frontend, env, DB, redis, and
# (if running) the live API + UI. Prints a PASS/WARN/FAIL line per check and exits
# non-zero if any hard check fails.
#
# Usage: scripts/setup/verify-all.sh [dev|prod]   (default: dev)
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

PROFILE="${1:-dev}"
BE_ENV="$(backend_env_file "$PROFILE")"
FE_ENV="$(frontend_env_file "$PROFILE")"

FAILED=0
pass() { printf "${C_GREEN}PASS${C_RESET} %s\n" "$*"; }
soft() { printf "${C_YELLOW}WARN${C_RESET} %s\n" "$*"; }
fail() { printf "${C_RED}FAIL${C_RESET} %s\n" "$*"; FAILED=$((FAILED+1)); }

echo "================ Verifying installation (profile: $PROFILE) ================"

# -------------------------------------------------------------------- Backend / Python
echo; info "Backend (Python)"
if [ -d "$BACKEND/.venv" ]; then
  pass ".venv exists"
  VENV_PY="$BACKEND/.venv/bin/python"
  if "$VENV_PY" -c 'import sys;exit(0 if sys.version_info[:2]>=(3,11) else 1)' 2>/dev/null; then
    pass "Python $("$VENV_PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])') (>=3.11)"
  else
    fail "venv Python is older than 3.11"
  fi
  MISSING=""
  for mod in fastapi uvicorn sqlalchemy alembic langgraph langchain_anthropic langfuse pydantic; do
    "$VENV_PY" -c "import $mod" 2>/dev/null || MISSING="$MISSING $mod"
  done
  [ -z "$MISSING" ] && pass "Core packages importable" || fail "Missing packages:$MISSING (run setup-all.sh)"
else
  fail ".venv missing — run scripts/setup/setup-all.sh"
fi

# ------------------------------------------------------------------------- Frontend
echo; info "Frontend (Node)"
command -v node >/dev/null 2>&1 && pass "node $(node --version)" || fail "node not found"
[ -d "$FRONTEND/node_modules" ] && pass "node_modules installed" || fail "node_modules missing — run setup-all.sh"

# ---------------------------------------------------------------------------- Env files
echo; info "Environment files"
if [ -f "$BE_ENV" ]; then
  pass "backend $(basename "$BE_ENV") present"
  for key in DATABASE_URL JWT_SECRET; do
    [ -n "$(get_env_value "$BE_ENV" "$key")" ] && pass "  $key set" || fail "  $key empty (required)"
  done
  [ -n "$(get_env_value "$BE_ENV" ANTHROPIC_API_KEY)" ] && pass "  ANTHROPIC_API_KEY set" \
    || soft "  ANTHROPIC_API_KEY empty (required to run real research)"
  [ -n "$(get_env_value "$BE_ENV" TAVILY_API_KEY)" ] && pass "  TAVILY_API_KEY set" \
    || soft "  TAVILY_API_KEY empty (web_search will return no results)"
else
  fail "backend $(basename "$BE_ENV") missing — run setup-fresh.sh"
fi
if [ -f "$FE_ENV" ]; then
  pass "frontend $(basename "$FE_ENV") present"
  [ -n "$(get_env_value "$FE_ENV" VITE_API_BASE_URL)" ] && pass "  VITE_API_BASE_URL set" \
    || soft "  VITE_API_BASE_URL empty (frontend will use default)"
else
  fail "frontend $(basename "$FE_ENV") missing — run setup-fresh.sh"
fi

# -------------------------------------------------------------------------------- DB
# Authoritative check: can we actually connect with the app's DATABASE_URL? (A bare port
# probe can mislead — a local Postgres may listen only on ::1 / a unix socket.)
echo; info "Database"
if [ -d "$BACKEND/.venv" ] && [ -f "$BE_ENV" ]; then
  DBURL="$(get_env_value "$BE_ENV" DATABASE_URL)"
  CHK="$("$BACKEND/.venv/bin/python" - "$DBURL" <<'PY' 2>&1
import sys
url = sys.argv[1].replace("+asyncpg", "").replace("+psycopg", "")
try:
    import psycopg
    with psycopg.connect(url, connect_timeout=5) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        n = cur.fetchone()[0]
    print(f"ok:{n}")
except Exception as e:
    print("err:" + str(e).splitlines()[0][:120]); sys.exit(1)
PY
)"
  if [[ "$CHK" == ok:* ]]; then
    NTABLES="${CHK#ok:}"
    pass "Connected to the database (${NTABLES} public tables)"
    if [ "${NTABLES:-0}" -ge 9 ]; then pass "Schema present (migrations applied)"; else soft "Few/no tables — run migrations (setup-fresh.sh)"; fi
  else
    fail "Cannot connect via DATABASE_URL — ${CHK#err:}"
    soft "Start your Postgres and/or run setup-fresh.sh"
  fi
else
  fail "Cannot check DB (need backend/.venv and $(basename "$BE_ENV"))"
fi

# ----------------------------------------------------------------------------- Redis
echo; info "Redis"
port_open 127.0.0.1 "$REDIS_PORT" && pass "Redis reachable on :${REDIS_PORT}" \
  || fail "Redis not reachable on :${REDIS_PORT} — run setup-fresh.sh"

# ----------------------------------------------------------------------- Live services
echo; info "Live services (optional — only if run-local.sh is running)"
if curl -fsS -m 4 "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
  pass "API /health responds on :${API_PORT}"
  curl -fsS -m 4 "http://localhost:${API_PORT}/api/models" >/dev/null 2>&1 \
    && pass "API /api/models responds" || soft "API /api/models did not respond"
else
  soft "API not responding on :${API_PORT} (start it with run-local.sh)"
fi
if curl -fsS -m 4 "http://localhost:${UI_PORT}" >/dev/null 2>&1; then
  pass "Frontend responds on :${UI_PORT}"
else
  soft "Frontend not responding on :${UI_PORT} (start it with run-local.sh)"
fi

# -------------------------------------------------------------------------- summary
echo; echo "============================================================================"
if [ "$FAILED" -eq 0 ]; then
  ok "All hard checks passed."
  exit 0
else
  err "$FAILED hard check(s) failed — see FAIL lines above."
  exit 1
fi
