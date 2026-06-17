"""Run executor (Milestones 2-4 glue).

Drives the compiled LangGraph with a Postgres checkpointer (resumable), the Langfuse
callback handler (one trace per run), streams node/agent events to Redis for SSE, persists
agents/sources/findings/reports to Postgres, and pushes verifier eval scores to Langfuse.

This is invoked by the background worker (out-of-band from the HTTP request).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.events import clear_heartbeat, heartbeat, is_cancelled, publish_event
from app.core.observability import compute_trace, get_langfuse_handler, push_eval_scores, resolve_trace_url, run_trace
from app.core.prompts import register_templates
from app.db.models import FindingRow, Report, Run, RunAgent, SourceRow, WorkflowPlanRow
from app.db.session import sync_session
from workflow.graph import build_graph, initial_state
from workflow.nodes.renderer import render_markdown

# Map node names to run status (§7 status enum).
_NODE_STATUS = {
    "planner": "planning",
    "research_agent": "researching",
    "aggregator": "synthesizing",
    "synthesizer": "synthesizing",
    "verifier": "verifying",
    "renderer": "verifying",
}


def _checkpointer():
    """Return (saver, conn) — a Postgres checkpointer for resumable runs and its owning
    connection (to close after the run), or (MemorySaver, None) if Postgres is unavailable."""
    settings = get_settings()
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver

        # Own the connection so it stays open for the whole run (the from_conn_string
        # context manager would close it as soon as it goes out of scope). Autocommit is
        # required by the PostgresSaver.
        sync_url = settings.database_url.replace("+asyncpg", "").replace("+psycopg", "")
        conn = psycopg.connect(sync_url, autocommit=True)
        saver = PostgresSaver(conn)
        saver.setup()
        return saver, conn
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver(), None


def _start_heartbeat(run_id: str) -> threading.Event:
    """Background daemon that refreshes the run's liveness beacon every few seconds,
    independent of graph progress (so the UI can distinguish 'alive but on a slow call'
    from 'worker died')."""
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                heartbeat(run_id, ttl=30)
            except Exception:
                pass
            stop.wait(8)

    threading.Thread(target=_loop, daemon=True).start()
    return stop


def execute_run(run_id: str) -> None:
    """Entry point called by the worker. Loads the run, executes the graph, persists output.
    Resumes from the last checkpoint when one exists (network-failure recovery)."""
    register_templates()
    settings = get_settings()

    with sync_session() as db:
        run = db.get(Run, _as_uuid(run_id))
        if not run:
            return
        subject, subject_type, task = run.subject, run.subject_type, run.task
        model_config = run.model_config_json or {}
        plan_override = run.plan.plan if (run.plan and run.plan.is_generated is False and run.plan.plan and run.plan.plan.get("_edited")) else None
        trace_id, trace_link = compute_trace(run_id)  # deterministic id + provisional URL
        run.status = "planning"
        run.error = None  # clear any error from a prior attempt
        run.langfuse_trace_id = trace_link
        db.commit()

    handler = get_langfuse_handler(run_id=run_id, subject=subject, subject_type=subject_type, tags=["deep-dd"])
    heartbeat(run_id, ttl=30)       # set synchronously NOW so reconcile never races this job
    hb_stop = _start_heartbeat(run_id)

    saver, cp_conn = _checkpointer()
    graph = build_graph(checkpointer=saver)
    config: Dict[str, Any] = {
        "recursion_limit": settings.recursion_limit,
        "configurable": {"thread_id": run_id},
        "callbacks": [handler] if handler else [],
    }

    # Resume from the last checkpoint if this run was interrupted partway (pending nodes
    # remain). Otherwise start fresh from the initial state.
    resume = False
    try:
        snap = graph.get_state(config)
        resume = bool(snap.values) and bool(snap.next)
    except Exception:
        resume = False
    if resume:
        graph_input: Any = None
        publish_event(run_id, {"node": "run", "status": "resuming", "run_id": run_id})
    else:
        graph_input = initial_state(
            run_id=run_id, subject=subject, subject_type=subject_type, task=task,
            model_config=model_config, plan_override=plan_override,
        )
        publish_event(run_id, {"node": "run", "status": "planning", "run_id": run_id})

    final_state: Dict[str, Any] = {}
    error: Optional[str] = None
    cancelled = False
    try:
        # One Langfuse trace per run wraps the whole graph; node/agent/tool spans nest under it.
        with run_trace(trace_id, run_id=run_id, subject=subject, subject_type=subject_type, tags=["deep-dd"]) as resolved_url:
            if resolved_url and resolved_url != trace_link:
                _set_trace_link(run_id, resolved_url)  # upgrade provisional URL to the real one
            # Dual stream: "updates" gives per-node/per-agent deltas for LIVE progress (each
            # research branch reports as it finishes); "values" gives the accumulated state.
            for mode, chunk in graph.stream(graph_input, config=config, stream_mode=["updates", "values"]):
                if mode == "values":
                    final_state = chunk
                    if is_cancelled(run_id):
                        cancelled = True
                        break
                    _budget_guard(run_id, chunk)
                else:  # "updates": {node_name: partial_state_update}
                    _handle_updates(run_id, chunk)
    except Exception as e:  # noqa: BLE001
        import traceback

        error = str(e)
        # Log the full stack to the worker log so failures are diagnosable (the DB/UI only
        # carry the short message).
        print(f"[run {run_id}] FAILED: {error}\n{traceback.format_exc()}", flush=True)
    finally:
        hb_stop.set()
        clear_heartbeat(run_id)
        if cp_conn is not None:
            cp_conn.close()
        # If the in-span resolution never upgraded past the provisional host (e.g. the SDK
        # hadn't fetched the project id yet), try once more now that the trace has flushed.
        if trace_id:
            final_url = resolve_trace_url(trace_id)
            if final_url and final_url != trace_link:
                _set_trace_link(run_id, final_url)

    # Persist whatever was produced — even on failure/cancel — so completed research,
    # sources and findings are never lost (child tables populate from the reducer channels).
    terminal = "cancelled" if cancelled else ("failed" if error else "done")
    _persist_outputs(run_id, final_state, status=terminal, error=error)

    if terminal == "done":
        push_eval_scores(run_id, final_state.get("final_report", {}).get("verification", {}))
    publish_event(run_id, {"node": "run", "status": terminal, "run_id": run_id, "error": error,
                           "verification": final_state.get("final_report", {}).get("verification", {})})


# --------------------------------------------------------------------------------------
def _handle_updates(run_id: str, chunk: Dict[str, Any]) -> None:
    """Process an 'updates' delta: {node_name: partial_state}. Emits each node's events live
    (so every research agent reports as it completes) and advances the run status."""
    for node, upd in (chunk or {}).items():
        if not isinstance(upd, dict):
            continue
        status = _NODE_STATUS.get(node)
        if status:
            _mark(run_id, status)
        for ev in upd.get("events", []) or []:
            publish_event(run_id, {**ev, "run_id": run_id})


def reconcile_orphaned_runs() -> None:
    """Mark runs that were executing when the worker died as failed (resumable).

    Called at worker startup. A run is 'orphaned' if its status is mid-flight (not queued,
    not terminal) but it has no live heartbeat — meaning no worker is currently driving it.
    Queued runs are left alone (RQ will pick them up). Failed orphans can be resumed from
    their checkpoint via POST /api/runs/{id}/resume.
    """
    from app.core.events import heartbeat_age

    active = ("planning", "resuming", "researching", "synthesizing", "verifying")
    with sync_session() as db:
        rows = db.execute(select(Run).where(Run.status.in_(active))).scalars().all()
        for run in rows:
            if heartbeat_age(str(run.id)) is None:  # no live worker
                run.status = "failed"
                run.error = "Interrupted (worker stopped or network failure). Resume to continue."
                run.finished_at = datetime.now(timezone.utc)
                publish_event(str(run.id), {"node": "run", "status": "failed",
                                            "error": run.error, "run_id": str(run.id)})
        db.commit()


def _budget_guard(run_id: str, chunk: Dict[str, Any]) -> None:
    settings = get_settings()
    cost = float(chunk.get("cost_usd", 0.0))
    if settings.run_budget_usd and cost > settings.run_budget_usd:
        publish_event(run_id, {"node": "run", "status": "budget_warning",
                               "cost_usd": cost, "budget_usd": settings.run_budget_usd, "run_id": run_id})


def _persist_outputs(run_id: str, state: Dict[str, Any], *, status: str, error: Optional[str] = None) -> None:
    """Persist run children + reports from the graph state. Reads per-agent results from the
    reducer channels (raw_outputs / sources / aggregated_findings) so partial progress is
    saved even when the run failed before the renderer assembled the final report."""
    rid = _as_uuid(run_id)
    raw = state.get("raw_report", {})
    final = state.get("final_report", {})
    verification = final.get("verification", {})

    # Per-agent outputs come from the reducer channel (populated after research), falling
    # back to the assembled raw report if present.
    agent_outputs = state.get("raw_outputs") or raw.get("agent_outputs", [])

    with sync_session() as db:
        run = db.get(Run, rid)
        if not run:
            return

        # Plan
        plan = state.get("plan")
        if plan:
            existing = db.query(WorkflowPlanRow).filter_by(run_id=rid).one_or_none()
            if existing:
                existing.plan = plan
                existing.is_generated = bool(plan.get("_is_generated"))
            else:
                db.add(WorkflowPlanRow(run_id=rid, plan=plan, is_generated=bool(plan.get("_is_generated"))))

        # Replace child rows (idempotent re-run safety).
        for model in (RunAgent, SourceRow, FindingRow, Report):
            db.execute(delete(model).where(model.run_id == rid))

        for ao in agent_outputs:
            db.add(RunAgent(
                run_id=rid, name=ao.get("agent", ""), role=ao.get("role", ""),
                model=ao.get("model", ""), status="done",
                narrative_markdown=ao.get("narrative_markdown", ""),
                findings=ao.get("findings", []), tool_calls=ao.get("tool_calls", []),
            ))

        for s in state.get("sources", []):
            db.add(SourceRow(
                run_id=rid, citation_id=s["id"], url=s.get("url", ""), title=s.get("title", ""),
                publisher=s.get("publisher", ""), retrieved_at=s.get("retrieved_at"),
                snippet=s.get("snippet", ""), content_hash=s.get("content_hash", ""),
                content=s.get("content", ""),
            ))

        for f in state.get("aggregated_findings", []):
            db.add(FindingRow(
                run_id=rid, agent=f.get("agent", ""), claim=f.get("claim", ""),
                source_ids=f.get("source_ids", []), confidence=f.get("confidence", "medium"),
                category=f.get("category"),
            ))

        # Persist reports only when the renderer produced them.
        if raw:
            db.add(Report(run_id=rid, kind="raw", report_json=raw,
                          report_markdown=render_markdown(raw, "raw"), verification={}))
        if final:
            db.add(Report(run_id=rid, kind="final", report_json=final,
                          report_markdown=render_markdown(final, "final"), verification=verification))

        run.status = status
        run.error = error[:4000] if error else None
        run.cost_usd = float(state.get("cost_usd", 0.0))
        run.finished_at = datetime.now(timezone.utc)
        db.commit()


def _set_trace_link(run_id: str, url: str) -> None:
    with sync_session() as db:
        run = db.get(Run, _as_uuid(run_id))
        if run:
            run.langfuse_trace_id = url
            db.commit()


def _mark(run_id: str, status: str, error: Optional[str] = None) -> None:
    with sync_session() as db:
        run = db.get(Run, _as_uuid(run_id))
        if not run:
            return
        run.status = status
        if error:
            run.error = error[:4000]
        if status in ("done", "failed", "cancelled"):
            run.finished_at = datetime.now(timezone.utc)
        db.commit()


def _as_uuid(run_id: str):
    import uuid

    return run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
