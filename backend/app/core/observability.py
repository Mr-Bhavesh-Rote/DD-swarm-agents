"""Langfuse v3 instrumentation (§10, Milestone 2).

One trace per run; nested spans per node/agent/tool come for free from the LangChain
CallbackHandler. Eval scores (citation coverage + faithfulness) are pushed via
create_score. Everything is a safe no-op when Langfuse is not configured.

Langfuse skill best practices applied:
- Explicit trace input/output (subject/task in, status/verification out)
- user_id on traces for cost attribution and user-level filtering
- session_id groups retries/resumes of the same run
- Descriptive trace names (subject, not generic ids)
- Explicit flush() before worker exits so traces are not lost
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional

from app.core.config import get_settings

try:
    from langfuse.langchain import CallbackHandler as _CallbackHandler
except Exception:  # pragma: no cover
    _CallbackHandler = None  # type: ignore[misc, assignment]


@lru_cache(maxsize=1)
def get_langfuse_client() -> Optional[Any]:
    """Return the singleton Langfuse client initialized from app settings.

    Pydantic-settings reads the .env file into the Settings object, but it does NOT export
    those values into os.environ. The Langfuse v3 `get_client()` helper looks at
    os.environ, so without an explicit initialization it creates a disabled client and
    traces silently disappear. We therefore initialize Langfuse with the Settings values
    directly on first use.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        return None


class _TraceAwareCallbackHandler(_CallbackHandler):  # type: ignore[valid-type, misc]
    """LangChain callback handler that forces root spans into a specific Langfuse trace.

    The stock Langfuse CallbackHandler creates a new trace for every top-level chain when
    parent_run_id is None. That means our manually-opened run trace stays empty while the
    actual LLM/agent observations end up in separate, hard-to-find traces. This wrapper
    subclasses the real CallbackHandler so it passes LangChain isinstance checks, then
    overrides only the root `on_chain_start` to re-root it onto the supplied `trace_id`.
    """

    def __init__(self, trace_id: str, public_key: Optional[str] = None):
        from uuid import UUID

        super().__init__(public_key=public_key)
        self._trace_id = trace_id
        self._uuid_type = UUID

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        from langfuse.types import TraceContext

        if parent_run_id is not None:
            return super().on_chain_start(
                serialized, inputs, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )
        try:
            run_id_uuid = run_id if isinstance(run_id, self._uuid_type) else self._uuid_type(run_id)
            self.runs[run_id_uuid] = self.client.start_span(
                name=self.get_langchain_run_name(serialized, **kwargs),
                input=inputs,
                trace_context=TraceContext(trace_id=self._trace_id),
            )
        except Exception:
            # Fallback to stock behavior if anything goes wrong.
            return super().on_chain_start(
                serialized, inputs, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )


def get_langfuse_handler(
    *, run_id: str, subject: str, subject_type: str, tags: Optional[List[str]] = None
) -> Optional[Any]:
    """Return a LangChain CallbackHandler that nests its spans under the run's trace.

    Calling get_langfuse_client() first ensures the global Langfuse singleton is initialized
    from our Settings; the returned handler is then bound to the run's deterministic trace_id.
    """
    client = get_langfuse_client()
    if not client:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        trace_id, _ = compute_trace(run_id)
        if not trace_id:
            return CallbackHandler()
        return _TraceAwareCallbackHandler(trace_id=trace_id)
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
    client = get_langfuse_client()
    if not client:
        return None, host
    try:
        return client.create_trace_id(seed=run_id), host
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
    client = get_langfuse_client()
    if not client:
        return None
    try:
        url = client.get_trace_url(trace_id=trace_id)
        if not url:
            print(f"[langfuse] trace url unresolved for {trace_id} (project id not yet fetched)", flush=True)
        return url
    except Exception as e:  # noqa: BLE001
        print(f"[langfuse] trace url resolution failed for {trace_id}: {e}", flush=True)
        return None


@contextmanager
def run_trace(
    trace_id: Optional[str], *, run_id: str, subject: str, subject_type: str, task: str,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    output: Optional[Dict[str, Any]] = None,
) -> Iterator[Optional[str]]:
    """Open one Langfuse trace for the whole run (forced to `trace_id`) so every node/agent/
    tool span nests under it.

    Best practices:
    - Trace name = subject (human-readable, filterable)
    - input = what we asked for (subject/task/config)
    - output = caller populates the `output` dict; we attach it before the trace closes
    - user_id = run owner for cost attribution and user-level filtering
    - session_id = run_id so retries/resumes group together
    - tags = subject_type + custom tags
    - metadata = planning_mode, model_config, etc.

    Yields the project-scoped trace URL (resolved here, in-span, so no warning) or None when
    Langfuse is disabled/unavailable. Flushes the SDK before the span exits so traces are not
    lost (a common mistake in background workers).
    """
    settings = get_settings()
    if not (settings.langfuse_enabled and trace_id):
        yield None
        return
    client = get_langfuse_client()
    if not client:
        yield None
        return
    try:
        from langfuse.types import TraceContext

        cm = client.start_as_current_span(
            name=f"deep-dd:{subject}", trace_context=TraceContext(trace_id=trace_id)
        )
    except Exception:
        yield None
        return
    with cm:
        url: Optional[str] = None
        try:
            client.update_current_trace(
                name=subject,
                user_id=user_id,
                session_id=run_id,
                input={
                    "subject": subject,
                    "subject_type": subject_type,
                    "task": task,
                    "metadata": metadata or {},
                },
                tags=(tags or []) + [subject_type, "deep-dd"],
            )
            url = resolve_trace_url(trace_id)  # in-span -> no "No active span" warning
        except Exception:
            pass
        try:
            yield url
        finally:
            # Attach final output while the trace span is still active, then flush.
            try:
                if output:
                    client.update_current_trace(output=output)
            except Exception:
                pass
            flush_langfuse()


def flush_langfuse() -> None:
    """Flush pending Langfuse events. Call before a worker process exits to ensure traces are
    not lost (common mistake: no flush() in scripts). Safe no-op when disabled."""
    settings = get_settings()
    if not settings.langfuse_enabled:
        return
    client = get_langfuse_client()
    if not client:
        return
    try:
        client.flush()
    except Exception:
        pass


def push_eval_scores(run_id: str, verification: Dict[str, Any],
                     quality_assessment: Optional[Dict[str, Any]] = None) -> None:
    """Push citation coverage, faithfulness, and quality score as Langfuse eval scores."""
    settings = get_settings()
    if not settings.langfuse_enabled or not verification:
        return
    client = get_langfuse_client()
    if not client:
        return
    try:
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
        # Quality score from the 4-gate framework.
        if quality_assessment:
            qg = quality_assessment.get("quality_gates", {})
            if qg:
                client.create_score(
                    name="quality_score",
                    value=float(qg.get("quality_score", 0)) / 100.0,
                    trace_id=trace_id,
                    session_id=run_id,
                    data_type="NUMERIC",
                )
        client.flush()
    except Exception:
        pass
