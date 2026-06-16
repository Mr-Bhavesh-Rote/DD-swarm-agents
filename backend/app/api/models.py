"""Model catalog endpoint (§4.3, §6 GET /api/models).

Server-driven so models can be added without a UI deploy.
"""
from __future__ import annotations

from fastapi import APIRouter

from workflow.models import MODEL_CATALOG

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
async def get_models() -> list[dict]:
    return MODEL_CATALOG
