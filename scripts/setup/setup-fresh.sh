#!/usr/bin/env bash
# setup-fresh.sh — prepare env files and bring the database up & migrated.
#
#   1. Create .env files from .env.example for both projects:
#        backend/.env-dev  + backend/.env       (dev / prod)
#        frontend/.env-dev + frontend/.env      (dev / prod)
#   2. Patch the DEV env with local values (CORS, generated JWT secret, frontend API URL).
#      DATABASE_URL/REDIS_URL are only set automatically when Docker will provide them and
#      the value is still the example placeholder — a custom DATABASE_URL is respected.
#   3. Bring the database up:
#        - Docker available + placeholder URL -> start Postgres + Redis via docker compose.
#        - Otherwise (external/local Postgres) -> use DATABASE_URL as-is and CREATE the
#          target database if it does not exist.
#   4. Run Alembic migrations against the resulting DATABASE_URL.
#
# Existing env files are NOT overwritten.
# Usage: scripts/setup/setup-fresh.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

PLACEHOLDER_DB="postgresql+asyncpg://user:pass@localhost:${DB_PORT}/deepdd"
DOCKER_DB_URL="postgresql+asyncpg://deepdd:deepdd@localhost:${DB_PORT}/deepdd"
DEV_REDIS_URL="redis://localhost:${REDIS_PORT}/0"

create_from_example() {  # <dir> <target-file>
  local dir="$1" target="$2" example
  example="$(env_example "$dir")" || die "No .env.example found in $dir"
  if [ -f "$target" ]; then
    ok "Exists, keeping: ${target#"$ROOT"/}"
  else
    cp "$example" "$target"
    ok "Created ${target#"$ROOT"/} from $(basename "$example")"
  fi
}

# Ensure the target database exists on an external/local Postgres, creating it if missing.
# Returns 0 if the DB exists or was created; 1 if the server could not be reached.
ensure_database() {  # <database_url>
  [ -x "$BACKEND/.venv/bin/python" ] || { warn "Backend .venv missing — cannot auto-create DB."; return 1; }
  "$BACKEND/.venv/bin/python" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, unquote
raw = sys.argv[1]
sync = raw.replace("+asyncpg", "").replace("+psycopg", "")
u = urlsplit(sync)
user = unquote(u.username or "")
pw = unquote(u.password or "")
host = u.hostname or "localhost"
port = u.port or 5432
dbname = (u.path or "/").lstrip("/") or "postgres"
try:
    import psycopg
except Exception as e:
    sys.stderr.write(f"psycopg not installed: {e}\n"); sys.exit(2)
# 1) Already there?
try:
    with psycopg.connect(host=host, port=port, user=user, password=pw, dbname=dbname, connect_timeout=5):
        print("exists"); sys.exit(0)
except Exception:
    pass
# 2) Connect to the maintenance DB and create it.
try:
    conn = psycopg.connect(host=host, port=port, user=user, password=pw, dbname="postgres",
                           connect_timeout=5, autocommit=True)
except Exception as e:
    sys.stderr.write(f"connect-failed: {e}\n"); sys.exit(1)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
        if cur.fetchone():
            print("exists")
        else:
            cur.execute(f'CREATE DATABASE "{dbname}"')
            print("created")
finally:
    conn.close()
PY
}

# ------------------------------------------------------------------- 1. create env files
info "Creating environment files (dev + prod) ..."
create_from_example "$BACKEND"  "$(backend_env_file dev)"
create_from_example "$BACKEND"  "$(backend_env_file prod)"
create_from_example "$FRONTEND" "$(frontend_env_file dev)"
create_from_example "$FRONTEND" "$(frontend_env_file prod)"

BE_DEV="$(backend_env_file dev)"
FE_DEV="$(frontend_env_file dev)"

# -------------------------------------------------------------- 2. decide DB mode + patch
CUR_DB="$(get_env_value "$BE_DEV" DATABASE_URL || true)"
is_placeholder() { [ -z "$CUR_DB" ] || [ "$CUR_DB" = "$PLACEHOLDER_DB" ]; }

if has_docker && is_placeholder; then
  DB_MODE="docker"
  info "Docker detected and DATABASE_URL is the placeholder — using docker compose Postgres."
  set_env_key "$BE_DEV" "DATABASE_URL" "$DOCKER_DB_URL"
  set_env_key "$BE_DEV" "REDIS_URL"    "$DEV_REDIS_URL"
else
  DB_MODE="external"
  if ! has_docker; then
    info "Docker not available — using the DATABASE_URL already in $(basename "$BE_DEV")."
  else
    info "Custom DATABASE_URL detected — respecting it (not switching to docker)."
  fi
  if is_placeholder; then
    warn "DATABASE_URL is unset/placeholder. Edit $(basename "$BE_DEV") to point at your local"
    warn "Postgres (e.g. postgresql+asyncpg://postgres:PASSWORD@localhost:${DB_PORT}/deepdd), then re-run."
  fi
fi

info "Patching DEV env (CORS, JWT secret, frontend API URL) ..."
set_env_key "$BE_DEV" "CORS_ALLOWED_ORIGINS" "http://localhost:${UI_PORT}"
if [ -z "$(get_env_value "$BE_DEV" JWT_SECRET || true)" ]; then
  set_env_key "$BE_DEV" "JWT_SECRET" "$(gen_secret)"
  ok "Generated a DEV JWT_SECRET."
fi
set_env_key "$FE_DEV" "VITE_API_BASE_URL" "http://localhost:${API_PORT}"
ok "DEV env patched."

if [ -z "$(get_env_value "$BE_DEV" ANTHROPIC_API_KEY || true)" ]; then
  warn "ANTHROPIC_API_KEY is empty in $(basename "$BE_DEV") — set it before running real research."
fi

DBURL="$(get_env_value "$BE_DEV" DATABASE_URL || true)"

# --------------------------------------------------------------- 3. bring the DB up
DB_READY=0
if [ "$DB_MODE" = "docker" ]; then
  info "Starting PostgreSQL + Redis via docker compose ..."
  compose up -d postgres redis
  info "Waiting for PostgreSQL on :${DB_PORT} ..."
  for _ in $(seq 1 30); do
    compose exec -T postgres pg_isready -U deepdd >/dev/null 2>&1 && break
    sleep 1
  done
  if compose exec -T postgres pg_isready -U deepdd >/dev/null 2>&1; then
    ok "PostgreSQL is ready."; DB_READY=1
  else
    warn "PostgreSQL did not become ready. Check: docker compose logs postgres"
  fi
  port_open 127.0.0.1 "$REDIS_PORT" && ok "Redis reachable on :${REDIS_PORT}." || warn "Redis not reachable on :${REDIS_PORT}."
else
  if [ -n "$DBURL" ] && [ "$DBURL" != "$PLACEHOLDER_DB" ]; then
    info "Ensuring the target database exists on your Postgres ..."
    if STATUS="$(ensure_database "$DBURL" 2>/tmp/deepdd_db_err)"; then
      [ "$STATUS" = "created" ] && ok "Database created." || ok "Database already exists."
      DB_READY=1
    else
      warn "Could not reach/create the database:"
      sed 's/^/    /' /tmp/deepdd_db_err 2>/dev/null || true
      warn "Make sure your local Postgres is running on :${DB_PORT} and the credentials in"
      warn "$(basename "$BE_DEV") DATABASE_URL are correct, then re-run this script."
    fi
    rm -f /tmp/deepdd_db_err
  fi
  port_open 127.0.0.1 "$REDIS_PORT" && ok "Redis reachable on :${REDIS_PORT}." \
    || warn "Redis not reachable on :${REDIS_PORT} — start Redis (jobs/SSE need it)."
fi

# --------------------------------------------------------------- 4. run migrations
if [ ! -d "$BACKEND/.venv" ]; then
  warn "Backend .venv not found — run scripts/setup/setup-all.sh, then re-run for migrations."
elif [ "$DB_READY" -ne 1 ]; then
  warn "Database not ready — skipping migrations. Once the DB is up, run:"
  warn "  cd backend && source .venv/bin/activate && set -a && source .env-dev && set +a && alembic upgrade head"
else
  info "Running Alembic migrations ..."
  (
    cd "$BACKEND"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    set -a; # shellcheck disable=SC1090
    source "$BE_DEV"; set +a
    alembic upgrade head
  )
  ok "Database schema is up to date."
fi

echo
warn "Production secrets in $(basename "$(backend_env_file prod)") are placeholders — fill them before deploying."
ok "setup-fresh complete."
info "Next: scripts/setup/run-local.sh   (start backend + worker + frontend)"
