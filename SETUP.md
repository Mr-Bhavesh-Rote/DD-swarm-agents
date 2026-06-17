# Setup & Operations Guide

Everything needed to install, configure, run, verify, and troubleshoot the platform. For
architecture and reference, see [README.md](README.md).

- [Prerequisites](#prerequisites)
- [Option A — local dev with helper scripts (recommended)](#option-a--local-dev-with-helper-scripts-recommended)
- [Option B — Docker Compose](#option-b--docker-compose)
- [Option C — manual setup](#option-c--manual-setup)
- [Environment files](#environment-files)
- [Database](#database)
- [Running & parallelism](#running--parallelism)
- [Headless CLI](#headless-cli)
- [Tests](#tests)
- [Tuning depth, speed & cost](#tuning-depth-speed--cost)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.11+** | required by LangGraph + Langfuse |
| Node.js | 18+ (20 recommended) | frontend (Vite) |
| PostgreSQL | 15+ | local install **or** Docker |
| Redis | 7+ | job queue, SSE pub/sub, heartbeats (local or Docker) |
| Docker | optional | only for Option B, or to provide Postgres/Redis |
| WeasyPrint native libs | for PDF export on macOS | `brew install pango cairo gdk-pixbuf libffi` |

**API keys you must supply:** `ANTHROPIC_API_KEY` (required), `TAVILY_API_KEY` (for web
search/scraping). `LANGFUSE_*` keys are optional (tracing).

---

## Option A — local dev with helper scripts (recommended)

All scripts live in `scripts/setup/` and resolve their own paths, so you can run them from
anywhere. From the repo root:

```bash
cd deep-dd

# 1) Install dependencies (creates backend/.venv, reuses it if present; npm install).
scripts/setup/setup-all.sh

# 2) Create env files, bring up the DB, run migrations.
scripts/setup/setup-fresh.sh

# 3) Fill in your secrets in backend/.env-dev:
#    ANTHROPIC_API_KEY=...   TAVILY_API_KEY=...   (LANGFUSE_* optional)

# 4) Start API + worker pool + frontend (backgrounded).
scripts/setup/run-local.sh            # dev profile by default

# 5) Verify everything is healthy.
scripts/setup/verify-all.sh

# Stop everything (DB/Redis keep running).
scripts/setup/stop-local.sh
```

| Script | What it does |
|---|---|
| `setup-all.sh` | Backend: find Python 3.11+, create/reuse `.venv`, `pip install -r requirements.txt`. Frontend: `npm install`. |
| `setup-fresh.sh` | Create `.env-dev` + `.env` for backend & frontend from `.env.example`; patch dev values (CORS, JWT secret, exports path, frontend API URL); bring up Postgres+Redis (Docker if available, else use your local Postgres) and **create the target DB if missing**; run `alembic upgrade head`. |
| `run-local.sh [dev\|prod]` | Start the API (uvicorn, `--reload` in dev), `WORKER_CONCURRENCY` background workers, and the Vite dev server. PIDs in `.local-run/`, logs in `.local-run/logs/`. |
| `stop-local.sh` | Stop API, all workers, and frontend (frees ports as a fallback). Leaves Docker DB/Redis up. |
| `verify-all.sh [dev\|prod]` | Check venv + Python version + core packages, node_modules, env files + required keys, a real DB connect (counts tables), Redis, and the live API/UI. Exits non-zero on any hard failure. |

Outputs (pids + logs) go to `deep-dd/.local-run/` (git-ignored):
`logs/api.log`, `logs/worker.<n>.log`, `logs/frontend.log`.

URLs: **API** http://localhost:8000 (`/docs`) · **Frontend** http://localhost:5173

---

## Option B — Docker Compose

Brings up postgres, redis, the API, a worker, the frontend, and runs migrations first.

```bash
cd deep-dd
cp backend/.env.example backend/.env     # fill ANTHROPIC_API_KEY, TAVILY_API_KEY, JWT_SECRET, LANGFUSE_*
docker compose up --build
# API: http://localhost:8000   Frontend: http://localhost:8080
```

Run multiple parallel workers:

```bash
docker compose up --scale worker=3
```

The compose Postgres uses `deepdd:deepdd`; the `migrate` service runs `alembic upgrade head`
before the API starts. Exports are written to a named volume at `/var/deepdd`.

---

## Option C — manual setup

**Backend**

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # fill in secrets
alembic upgrade head                 # create schema (DB must be reachable)
uvicorn app.main:app --reload        # API on :8000
python worker.py                     # background worker (separate shell; one per parallel run)
```

**Frontend**

```bash
cd frontend
npm install
cp .env.example .env                 # VITE_API_BASE_URL=http://localhost:8000
npm run dev                          # Vite on :5173
```

Register a user via the UI (or `POST /api/auth/register`) to get a token.

---

## Environment files

The backend reads a single `.env` file (typed in `app/core/config.py`) and **fails fast**
on missing required keys (`ANTHROPIC_API_KEY`, `DATABASE_URL`, `JWT_SECRET`).

`setup-fresh.sh` creates a **dev** and a **prod** profile per project:

| File | Profile | Used by |
|---|---|---|
| `backend/.env-dev`, `frontend/.env-dev` | development | `run-local.sh dev`, migrations |
| `backend/.env`, `frontend/.env` | production | `run-local.sh prod`, Docker |

The dev profile is loaded by `run-local.sh`/migrations by exporting it into the environment
(env vars take precedence over the `.env` file in pydantic-settings). Fill secrets in
`backend/.env-dev` for local work.

> **Secrets hygiene:** `.env`, `.env-dev`, and `.local-run/` are git-ignored. Keep
> `.env.example` to placeholders only — don't commit real keys there.

See the [Configuration table in README.md](README.md#configuration-env) for every key.

---

## Database

`setup-fresh.sh` handles the DB automatically:

- **Docker available + placeholder `DATABASE_URL`** → starts the compose Postgres
  (`deepdd:deepdd`) and points dev at it.
- **No Docker / custom `DATABASE_URL`** → respects your existing `DATABASE_URL` and
  **creates the target database if it doesn't exist** (via psycopg, using those creds).

For a local Postgres, set `DATABASE_URL` in `.env.example`/`.env-dev`, e.g.:

```
DATABASE_URL=postgresql+asyncpg://postgres:YOURPASS@localhost:5432/deepdd
```

Manual migration any time:

```bash
cd backend && source .venv/bin/activate
set -a && source .env-dev && set +a
alembic upgrade head
```

> Tip: a local Postgres may listen only on IPv6/socket; `verify-all.sh` confirms the DB by
> actually connecting (and counting tables), not just probing a port.

---

## Running & parallelism

Runs execute in a **pool of background workers** — one job per worker at a time, so the pool
size = number of reports that run concurrently. Default is **3**.

```bash
WORKER_CONCURRENCY=5 scripts/setup/run-local.sh dev   # 5 parallel runs
```

Watch progress: open a run in the UI (live swarm view via SSE) or tail
`.local-run/logs/worker.<n>.log`. A failed/interrupted run can be **Resumed** from the UI —
it continues from the last checkpoint.

---

## Headless CLI

Run the full workflow without the API/DB/UI — writes `out/<run_id>/{raw,final}.{json,md}`:

```bash
cd backend && source .venv/bin/activate
python cli.py --subject "Anunta Technology Management Services Limited" --type company
python cli.py --subject "Jane Doe" --type individual --model claude-sonnet-4-6
```

Only needs `ANTHROPIC_API_KEY` (+ `TAVILY_API_KEY` for real search).

---

## Tests

```bash
cd backend && source .venv/bin/activate
pytest tests/ -q
```

Covers citation dedup, config-loader fail-fast (cycles/unknown tools), model-resolution
precedence, verifier claim extraction, tolerant JSON parsing, and the `RunRequest` alias.

---

## Tuning depth, speed & cost

All in `.env-dev` (or `.env`). Restart workers after changing (`stop-local.sh` →
`run-local.sh`).

**For richer / longer reports:**
- `RESEARCH_MAX_TOKENS` ↑ (e.g. 12000–16000) — longer per-agent RAW output.
- `SYNTHESIZER_MAX_TOKENS` ↑ (e.g. 24000–32000) — longer FINAL report.
- `SEARCH_MAX_RESULTS` ↑ — more sources per query.
- `SEARCH_DEPTH=advanced` — deeper Tavily crawl (slower).

**For faster / cheaper runs:**
- `SEARCH_DEPTH=basic` (default) — fastest search.
- Lower `RESEARCH_MAX_TOKENS` / `SEARCH_MAX_RESULTS`.
- Lower each agent's `max_iterations` in `config/agents.*.yaml`.
- Use a faster research model (`RESEARCH_MODEL=claude-haiku-4-5-20251001`) or set it per-run
  in the UI.

> Note: web search returns **compact** results (title/url/snippet) to the model while the
> full page text is stored for the verifier — so grounding stays rich without bloating the
> LLM context.

---

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Startup error: missing required configuration | A required key (`ANTHROPIC_API_KEY` / `DATABASE_URL` / `JWT_SECRET`) is empty. Set it in `backend/.env-dev`. |
| `role "deepdd" does not exist` on migrate | Your local Postgres uses different creds than the compose default. Set `DATABASE_URL` in `.env-dev` to your real creds and re-run `setup-fresh.sh` (it creates the DB). |
| `ModuleNotFoundError: email_validator` | `pip install -r requirements.txt` (it's pinned). |
| `the greenlet library is required` | Same — reinstall requirements (greenlet is pinned for SQLAlchemy async). |
| bcrypt "password cannot be longer than 72 bytes" | Fixed in code (direct bcrypt). Reinstall requirements if you see it. |
| Worker aborts with `objc[...] fork()` / signal 6 (macOS) | Handled — the worker uses `SimpleWorker` on macOS. Ensure you start via `run-local.sh` / `python worker.py`. |
| Run stuck in `planning`, status never advances | `RECURSION_LIMIT` too low. It must be ≥ ~10; set it to **50** in `.env-dev`. |
| `Request timed out` mid-run | Raise `LLM_TIMEOUT_SECONDS` (default 120). Transient blips auto-retry (`LLM_MAX_RETRIES`). |
| `temperature is deprecated for this model` | Handled — Opus/Fable models omit `temperature` automatically. |
| `'list' object has no attribute 'get'` | Handled — nodes tolerate array-shaped model output. If it recurs, the full traceback is now in `worker.<n>.log`. |
| Export returns **401** | Use the in-app Export button (sends the auth header). Direct `<a href>` links can't authenticate. |
| Export returns **500** `/var/deepdd` permission denied | `EXPORT_STORAGE_URI` not writable. `setup-fresh.sh` points dev at `.local-run/exports`; archival is best-effort and won't block the download. |
| Langfuse link 404 | Fixed — links are now project-scoped (`/project/{id}/traces/{id}`) and resolve once the run produces spans. Resume an old failed run to refresh its link. |
| `Context error: No active span` in worker log | Harmless Langfuse log; resolved by building the trace URL in-span. No action needed. |
| Resume button stays after resuming | Fixed — the UI uses the polled DB status as authoritative and reconnects the SSE stream on resume. |
| DB/UI "not reachable" in verify but it's up | A local Postgres on IPv6/socket; `verify-all.sh` connects via the driver, not a raw port probe — re-run it. |
| Research very slow | `SEARCH_DEPTH=basic`, lower `*_MAX_TOKENS` / `SEARCH_MAX_RESULTS` / agent `max_iterations`. See tuning above. |
| Need more parallel reports | `WORKER_CONCURRENCY=N scripts/setup/run-local.sh` (local) or `docker compose up --scale worker=N`. |
