"""Langfuse v3 instrumentation (§10, Milestone 2).

One trace per run; nested spans per node/agent/tool come for free from the LangChain
CallbackHandler. Eval scores (citation coverage + faithfulness) are pushed via
create_score. Everything is a safe no-op when Langfuse is not configured.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.config import get_settings


def get_langfuse_handler(
    *, run_id: str, subject: str, subject_type: str, tags: Optional[List[str]] = None
) -> Optional[Any]:
    """Return a LangChain CallbackHandler, or None if disabled.

    In langfuse v3 the handler takes no constructor args (keys are read from the
    environment); trace attributes (session id, tags, metadata) are attached via the run
    config — see `trace_config`.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:
        return None


def trace_config(
    handler: Optional[Any], *, run_id: str, subject: str, subject_type: str,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the LangGraph config fragment with callbacks + Langfuse trace metadata.

    Langfuse v3 reads `langfuse_session_id` / `langfuse_tags` and arbitrary metadata from
    the runnable config's `metadata` to scope and tag the trace (one trace per run)."""
    if not handler:
        return {}
    return {
        "callbacks": [handler],
        "metadata": {
            "langfuse_session_id": run_id,
            "langfuse_tags": (tags or []) + [subject_type],
            "run_id": run_id,
            "subject": subject,
            "subject_type": subject_type,
        },
    }


def push_eval_scores(run_id: str, verification: Dict[str, Any]) -> None:
    """Push citation coverage + faithfulness as Langfuse eval scores (§4.2)."""
    settings = get_settings()
    if not settings.langfuse_enabled or not verification:
        return
    try:
        from langfuse import get_client

        client = get_client()
        client.create_score(
            name="citation_coverage",
            value=float(verification.get("citation_coverage", 0.0)),
            session_id=run_id,
            data_type="NUMERIC",
        )
        client.create_score(
            name="faithfulness",
            value=float(verification.get("faithfulness_score", 0.0)),
            session_id=run_id,
            data_type="NUMERIC",
        )
        client.flush()
    except Exception:
        pass


def trace_url(run_id: str) -> str:
    """Best-effort Langfuse deep-link for a run (filtered by session id)."""
    settings = get_settings()
    host = settings.langfuse_host.rstrip("/")
    return f"{host}/sessions/{run_id}"
