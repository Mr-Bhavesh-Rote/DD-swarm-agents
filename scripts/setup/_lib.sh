#!/usr/bin/env bash
# Shared helpers for the setup scripts. Source this from each script.
# Resolves project paths, logging, env-file handling and small utilities.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$LIB_DIR/../.." && pwd)"          # deep-dd/
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
COMPOSE_FILE="$ROOT/docker-compose.yml"
RUN_DIR="$ROOT/.local-run"                     # runtime pids + logs (git-ignored)
LOG_DIR="$RUN_DIR/logs"

API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"
DB_PORT="${DB_PORT:-5432}"
REDIS_PORT="${REDIS_PORT:-6379}"
# Number of worker processes = max runs that execute in parallel (RQ gives each worker
# one job at a time). Override: WORKER_CONCURRENCY=3 scripts/setup/run-local.sh
WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-3}"

# --- logging ---
if [ -t 1 ]; then
  C_RESET="\033[0m"; C_BLUE="\033[34m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_RED="\033[31m"
else
  C_RESET=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi
info() { printf "${C_BLUE}==>${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}✓${C_RESET}  %s\n" "$*"; }
warn() { printf "${C_YELLOW}!${C_RESET}  %s\n" "$*"; }
err()  { printf "${C_RED}✗${C_RESET}  %s\n" "$*" >&2; }
die()  { err "$*"; exit 1; }

# --- profile -> env file name ---
# Usage: backend_env_file <dev|prod>
backend_env_file()  { [ "${1:-dev}" = "prod" ] && echo "$BACKEND/.env"  || echo "$BACKEND/.env-dev"; }
frontend_env_file() { [ "${1:-dev}" = "prod" ] && echo "$FRONTEND/.env" || echo "$FRONTEND/.env-dev"; }

# Locate a project's env example (.env.example or .env-example).
env_example() {
  local dir="$1"
  if [ -f "$dir/.env.example" ]; then echo "$dir/.env.example";
  elif [ -f "$dir/.env-example" ]; then echo "$dir/.env-example";
  else return 1; fi
}

# --- python selection (3.11+ required) ---
pick_python() {
  local c ver
  for c in python3.11 python3.12 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      ver="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
      if "$c" -c 'import sys;exit(0 if sys.version_info[:2]>=(3,11) else 1)'; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

# --- docker compose wrapper (v2 plugin or legacy binary) ---
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "$COMPOSE_FILE" "$@"
  else
    return 127
  fi
}
has_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# --- TCP port reachability (bash /dev/tcp, works on macOS) ---
# Tries the given host plus IPv4/IPv6 loopback, since a local Postgres/Redis may listen
# only on ::1 (IPv6) and an IPv4-only probe would falsely report it as down.
port_open() {
  local host="${1:-localhost}" port="$2" h
  for h in "$host" 127.0.0.1 ::1 localhost; do
    (exec 3<>"/dev/tcp/${h}/${port}") >/dev/null 2>&1 && { exec 3>&-; return 0; }
  done
  return 1
}

# Set or append KEY=value in an env file (macOS/BSD sed compatible).
set_env_key() {
  local file="$1" key="$2" val="$3" esc
  esc="$(printf '%s' "$val" | sed -e 's/[\/&|]/\\&/g')"
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    sed -i '' -E "s|^${key}=.*|${key}=${esc}|" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

# Read a single value from an env file (sources in a subshell; honors inline comments).
get_env_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  ( set -a; # shellcheck disable=SC1090
    source "$file" >/dev/null 2>&1 || true
    set +a
    printf '%s' "${!key:-}" )
}

# Generate a random secret.
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32;
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

mkdir -p "$RUN_DIR" "$LOG_DIR"
