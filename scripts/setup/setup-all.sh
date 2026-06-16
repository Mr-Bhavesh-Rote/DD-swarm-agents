#!/usr/bin/env bash
# setup-all.sh — install dependencies for both projects.
#   Backend (Python): create a .venv if missing (reuse if present), then install
#                     requirements.txt into it.
#   Frontend (Node):  npm install.
#
# Usage: scripts/setup/setup-all.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

# ----------------------------------------------------------------------------- backend
info "Setting up backend (Python) ..."
PY="$(pick_python)" || die "Python 3.11+ is required but was not found on PATH."
info "Using Python: $("$PY" --version 2>&1) ($PY)"

cd "$BACKEND"
if [ -d .venv ]; then
  ok "Found existing virtual env (.venv) — reusing it."
else
  info "Creating virtual env (.venv) ..."
  "$PY" -m venv .venv
  ok "Created .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
info "Upgrading pip / wheel ..."
python -m pip install --upgrade pip wheel >/dev/null
info "Installing backend dependencies (this can take a few minutes) ..."
if python -m pip install -r requirements.txt; then
  ok "Backend dependencies installed."
else
  warn "Some backend packages failed to install."
  warn "WeasyPrint needs native libs on macOS: 'brew install pango cairo gdk-pixbuf libffi'."
  deactivate || true
  exit 1
fi
deactivate || true

# ---------------------------------------------------------------------------- frontend
info "Setting up frontend (Node) ..."
command -v npm >/dev/null 2>&1 || die "npm is required but was not found on PATH (install Node 18+)."
info "Using Node: $(node --version 2>&1), npm $(npm --version 2>&1)"

cd "$FRONTEND"
if [ -d node_modules ]; then
  ok "Found existing node_modules — refreshing with npm install."
fi
npm install
ok "Frontend dependencies installed."

echo
ok "setup-all complete."
info "Next: scripts/setup/setup-fresh.sh   (create .env files + bring up the DB)"
