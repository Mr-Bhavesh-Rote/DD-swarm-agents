"""FastAPI application entrypoint (§6).

Validates required config and fails fast on startup; registers the Langfuse prompt
templates; mounts all routers; configures CORS for the Vite frontend.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, models, runs, uploads
from app.core.config import REQUIRED_FOR_SERVER, get_settings, validate_required
from app.core.prompts import register_templates
from app.core.ratelimit import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    validate_required(settings, REQUIRED_FOR_SERVER)  # fail fast on missing keys
    register_templates()  # idempotent Langfuse prompt registration (no-op if disabled)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Deep Due-Diligence Research Platform", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware, max_requests=120, window_seconds=60)

    app.include_router(auth.router)
    app.include_router(runs.router)
    app.include_router(models.router)
    app.include_router(uploads.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(_request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


app = create_app()
