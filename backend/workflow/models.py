"""Model resolution (§4.3) and model catalog (§4.3 / GET /api/models).

Precedence (highest first):
  per-agent override (plan.agents[].model)
  -> per-role default (model_config.role_overrides[role])
  -> run-level global default (model_config.global_default)
  -> system default (settings.default_model, i.e. claude-opus-4-8)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.config import get_settings

# Server-driven catalog so models can be added without a UI deploy (§4.3).
MODEL_CATALOG: List[Dict[str, Any]] = [
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "tier": "reasoning",
     "recommended_roles": ["orchestrator", "writer", "verifier"]},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "tier": "balanced",
     "recommended_roles": ["research", "aggregator"]},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "tier": "fast",
     "recommended_roles": ["research"]},
    {"id": "claude-fable-5", "label": "Claude Fable 5", "tier": "frontier",
     "recommended_roles": ["orchestrator", "writer"]},
]

VALID_MODEL_IDS = {m["id"] for m in MODEL_CATALOG}


def resolve_model(
    *,
    role: str,
    model_config: Optional[Dict[str, Any]] = None,
    agent_model: Optional[str] = None,
) -> str:
    """Resolve the model id for a node/agent following the §4.3 precedence."""
    settings = get_settings()
    cfg = model_config or {}
    role_overrides = cfg.get("role_overrides") or {}

    candidates = [
        role_overrides.get(role),         # user per-role override (UI selection)
        cfg.get("global_default"),        # user run-level global default (UI selection)
        agent_model,                      # per-agent override (YAML/plan)
    ]
    for c in candidates:
        if c:
            return c

    # Role-aware system defaults, then the global system fallback.
    role_default = {
        "research": settings.research_model,
        "aggregator": settings.research_model,
        "verifier": settings.verifier_model,
    }.get(role)
    return role_default or settings.default_model


# Newer reasoning/frontier models deprecate the `temperature` parameter and return a 400
# if it is sent. Omit it for those; balanced/fast models still accept it.
NO_TEMPERATURE_MODELS = {"claude-opus-4-8", "claude-fable-5"}


def make_chat_model(model_id: str, *, temperature: float = 0.0, max_tokens: int = 4096):
    """Construct a langchain-anthropic ChatAnthropic for the given model id."""
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    kwargs = {
        "model": model_id,
        "max_tokens": max_tokens,
        "api_key": settings.anthropic_api_key,
        "timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
    }
    if model_id not in NO_TEMPERATURE_MODELS:
        kwargs["temperature"] = temperature
    return ChatAnthropic(**kwargs)
