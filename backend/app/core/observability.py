"""Langfuse v3 instrumentation (§10, Milestone 2).

One trace per run; nested spans per node/agent/tool come for free from the LangChain
CallbackHandler. Eval scores (citation coverage + faithfulness) are pushed via
create_score. Everything is a safe no-op when Langfuse is not configured.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.core.config import get_settings


def get_langfuse_handler(
    *, run_id: str, subject: str, subject_type: str, tags: Optional[List[str]] = None
) -> Optional[Any]:
    """Return a LangChain CallbackHandler, or None if disabled. The handler nests its spans
    under whatever trace is active (we open one explicitly in `run_trace`)."""
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:
        return None


def compute_trace(run_id: str) -> tuple[Optional[str], str]:
    """Return (trace_id, provisional_url).

    The trace id is derived deterministically from the run id (so the run's spans are forced
    onto it in `run_trace`). The real project-scoped URL is resolved later, INSIDE the trace
    span (avoids the SDK's "No active span" warning); until then we use the host as a
    provisional link. Returns (None, host) when Langfuse is disabled/unavailable.
    """
    settings = get_settings()
    host = settings.langfuse_host.rstrip("/")
    if not settings.langfuse_enabled:
        return None, host
    try:
        from langfuse import get_client

        return get_client().create_trace_id(seed=run_id), host
    except Exception:
        return None, host


def resolve_trace_url(trace_id: Optional[str]) -> Optional[str]:
    """Resolve the project-scoped trace deep-link (.../project/<id>/traces/<trace_id>).

    `get_trace_url` returns None until the SDK has fetched the project id (an API call), so
    callers may need to retry — we log a warning instead of swallowing silently, so a run that
    never upgrades past the bare host is diagnosable. Returns None when Langfuse is off or the
    project id is not yet available.
    """
    settings = get_settings()
    if not (settings.langfuse_enabled and trace_id):
        return None
    try:
        from langfuse import get_client

        url = get_client().get_trace_url(trace_id=trace_id)
        if not url:
            print(f"[langfuse] trace url unresolved for {trace_id} (project id not yet fetched)", flush=True)
        return url
    except Exception as e:  # noqa: BLE001
        print(f"[langfuse] trace url resolution failed for {trace_id}: {e}", flush=True)
        return None


@contextmanager
def run_trace(
    trace_id: Optional[str], *, run_id: str, subject: str, subject_type: str,
    tags: Optional[List[str]] = None,
) -> Iterator[Optional[str]]:
    """Open one Langfuse trace for the whole run (forced to `trace_id`) so every node/agent/
    tool span nests under it. Yields the project-scoped trace URL (resolved here, in-span, so
    no warning) or None when Langfuse is disabled/unavailable."""
    settings = get_settings()
    if not (settings.langfuse_enabled and trace_id):
        yield None
        return
    try:
        from langfuse import get_client
        from langfuse.types import TraceContext

        client = get_client()
        cm = client.start_as_current_span(
            name="deep-dd-run", trace_context=TraceContext(trace_id=trace_id)
        )
    except Exception:
        yield None
        return
    with cm:
        url: Optional[str] = None
        try:
            client.update_current_trace(
                session_id=run_id, name=subject, tags=(tags or []) + [subject_type]
            )
            url = resolve_trace_url(trace_id)  # in-span → no "No active span" warning
        except Exception:
            pass
        yield url


def push_eval_scores(run_id: str, verification: Dict[str, Any]) -> None:
    """Push citation coverage + faithfulness as Langfuse eval scores (§4.2)."""
    settings = get_settings()
    if not settings.langfuse_enabled or not verification:
        return
    try:
        from langfuse import get_client

        client = get_client()
        # Attach to BOTH the trace (so scores show on the run's trace view) and the session.
        trace_id = client.create_trace_id(seed=run_id)
        client.create_score(
            name="citation_coverage",
            value=float(verification.get("citation_coverage", 0.0)),
            trace_id=trace_id,
            session_id=run_id,
            data_type="NUMERIC",
        )
        client.create_score(
            name="faithfulness",
            value=float(verification.get("faithfulness_score", 0.0)),
            trace_id=trace_id,
            session_id=run_id,
            data_type="NUMERIC",
        )
        client.flush()
    except Exception:
        pass
