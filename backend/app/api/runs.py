"""Run lifecycle endpoints (§6).

All run-creating endpoints enqueue a background job and return immediately. Reports are
served from reports.report_json (the single source of truth). SSE streams progress.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, require_role
from app.core.config import get_settings
from app.core.events import heartbeat_age, request_cancel, subscribe_events
from app.db.models import Export, Report, Run, RunAgent, SourceRow, WorkflowPlanRow
from app.db.session import get_db
from app.schemas.contracts import RunRequest, TaskRefineRequest, TaskRefineResponse, WorkflowPlan
from workflow.config_loader import ConfigError, load_plan_for_subject, normalize_plan

router = APIRouter(prefix="/api/runs", tags=["runs"])


# --------------------------------------------------------------------------------------
# Create / list / get
# --------------------------------------------------------------------------------------
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(
    req: RunRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_role("analyst")),
) -> dict:
    run = Run(
        subject=req.subject,
        subject_type=req.subject_type,
        task=req.task,
        status="queued",
        planning_mode=req.planning_mode,
        max_research_agents=req.max_research_agents,
        model_config_json=req.model_config_.model_dump(),
        created_by=uuid.UUID(user["sub"]) if user.get("sub") else None,
    )
    db.add(run)
    await db.flush()

    # AI-tailored runs build the swarm synchronously so the analyst can review/approve it
    # BEFORE any expensive research runs (§10 gate). The run is held in "awaiting_plan" and
    # only enqueued once the plan is approved. If generation fails we fall back to the normal
    # flow (enqueue now; the planner regenerates at run time) so create never hard-fails.
    gated = False
    if req.plan_override is None and req.planning_mode == "ai":
        ai_plan, plan_cost = await asyncio.get_event_loop().run_in_executor(
            None, _generate_ai_plan, req
        )
        if ai_plan is not None:
            payload = ai_plan.model_dump()
            payload["_is_generated"] = True
            db.add(WorkflowPlanRow(run_id=run.id, plan=payload, is_generated=True, approved=False))
            run.status = "awaiting_plan"
            run.cost_usd = plan_cost  # seed with planning cost; the run folds research on top
            gated = True
    else:
        plan = _resolve_plan(req)
        if plan is not None:
            db.add(WorkflowPlanRow(run_id=run.id, plan=plan.model_dump(), is_generated=False))

    await db.commit()
    await db.refresh(run)

    if gated:
        return {"run_id": str(run.id), "status": "awaiting_plan"}

    # Enqueue out-of-band job (deferred import avoids a hard redis dep at import time).
    from worker import enqueue_run

    enqueue_run(str(run.id))
    return {"run_id": str(run.id), "status": "queued"}


def _resolve_plan(req: RunRequest) -> Optional[WorkflowPlan]:
    if req.plan_override is not None:
        return req.plan_override
    try:
        return load_plan_for_subject(req.subject_type, task=req.task)
    except ConfigError:
        return None  # planner will generate one at run time


def _generate_ai_plan(req: RunRequest) -> tuple[Optional[WorkflowPlan], float]:
    """Build an LLM-tailored plan from the task (mirrors planner path 3) so it can be
    reviewed before research. Returns (plan, cost_usd); plan is None on any failure → caller
    degrades to run-time generation. The per-run cap can only tighten the system MAX_SUBAGENTS."""
    from app.core.prompts import build_orchestrator_prompt
    from workflow.llm import invoke_json
    from workflow.models import resolve_model

    settings = get_settings()
    max_agents = min(req.max_research_agents or settings.max_subagents, settings.max_subagents)
    model_config = req.model_config_.model_dump()
    model = resolve_model(role="orchestrator", model_config=model_config)
    sys = build_orchestrator_prompt(req.subject, req.subject_type, req.task, max_agents)
    user = f"Subject: {req.subject}\nSubject type: {req.subject_type}\nTask: {req.task}"
    try:
        result = invoke_json(model, sys, user, max_tokens=4096)
        data = result.get("data") or {}
        if not data.get("agents"):
            return None, 0.0
        plan = normalize_plan(data)
        from workflow.nodes.planner import _ensure_domain_coverage

        plan = _ensure_domain_coverage(plan, req.subject, req.subject_type, max_agents)
        return plan, float(result.get("cost_usd", 0.0))
    except Exception:
        return None, 0.0


@router.post("/refine-task", response_model=TaskRefineResponse)
async def refine_task(req: TaskRefineRequest, _user=Depends(require_role("analyst"))) -> TaskRefineResponse:
    """Expand a plain-English ask into a structured DD task prompt (one cheap LLM call).
    Returned text is editable in the UI before the run is created."""
    from app.core.prompts import build_task_refine_prompt
    from workflow.llm import invoke_json
    from workflow.models import resolve_model

    if not req.query.strip():
        raise HTTPException(422, "query is required")
    model = resolve_model(role="orchestrator")
    sys = build_task_refine_prompt(req.subject_type)
    user = f"Subject: {req.subject or '(unspecified)'}\nSubject type: {req.subject_type}\nRequest: {req.query}"
    result = invoke_json(model, sys, user, max_tokens=1500)
    task = (result.get("data") or {}).get("task", "").strip()
    if not task:
        raise HTTPException(502, "Could not refine the task; please edit it manually.")
    return TaskRefineResponse(task=task, cost_usd=result.get("cost_usd", 0.0))


@router.get("")
async def list_runs(
    subject_type: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    _user=Depends(current_user),
) -> dict:
    stmt = select(Run)
    if subject_type:
        stmt = stmt.where(Run.subject_type == subject_type)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(Run.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    items = []
    for r in rows:
        coverage = await _coverage_for(db, r.id)
        items.append(_run_summary(r, coverage))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(current_user)) -> dict:
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    coverage = await _coverage_for(db, run_id)
    final = (await db.execute(select(Report).where(Report.run_id == run_id, Report.kind == "final"))).scalar_one_or_none()
    summary = _run_summary(run, coverage)
    summary["verification"] = final.verification if final else {}
    summary["quality_assessment"] = (final.report_json or {}).get("quality_assessment", {}) if final else {}
    summary["model_config"] = run.model_config_json
    # Liveness: a fresh heartbeat (< ~30s) means a worker is actively driving this run;
    # a non-terminal status with no heartbeat means it stalled/died (resumable).
    age = heartbeat_age(str(run_id))
    summary["heartbeat_age"] = age
    summary["alive"] = age is not None and age < 30
    return summary


# --------------------------------------------------------------------------------------
# Plan get/edit (§5.7 round-trip)
# --------------------------------------------------------------------------------------
@router.get("/{run_id}/plan")
async def get_plan(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(current_user)) -> dict:
    row = (await db.execute(select(WorkflowPlanRow).where(WorkflowPlanRow.run_id == run_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Plan not found")
    return row.plan


@router.put("/{run_id}/plan")
async def update_plan(
    run_id: uuid.UUID,
    plan: dict,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_role("analyst")),
) -> dict:
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("queued", "planning", "awaiting_plan"):
        raise HTTPException(409, "Plan can only be edited before research starts")
    try:
        normalized = normalize_plan(plan)  # validates references/cycles/tools (fail-fast)
    except ConfigError as e:
        raise HTTPException(422, str(e))
    payload = normalized.model_dump()
    payload["_edited"] = True
    row = (await db.execute(select(WorkflowPlanRow).where(WorkflowPlanRow.run_id == run_id))).scalar_one_or_none()
    if row:
        row.plan = payload
        row.approved = True
    else:
        db.add(WorkflowPlanRow(run_id=run_id, plan=payload, is_generated=False, approved=True))
    await db.commit()
    return payload


@router.post("/{run_id}/approve-plan")
async def approve_plan(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(require_role("analyst"))) -> dict:
    """Approve a held AI-tailored plan and start research (§10 gate). Only valid while the
    run is awaiting plan approval; flips it to queued and enqueues the worker job."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status != "awaiting_plan":
        raise HTTPException(409, "This run is not awaiting plan approval")
    row = (await db.execute(select(WorkflowPlanRow).where(WorkflowPlanRow.run_id == run_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(409, "No plan to approve")
    row.approved = True
    run.status = "queued"
    await db.commit()

    from worker import enqueue_run

    enqueue_run(str(run_id))
    return {"run_id": str(run_id), "status": "queued"}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(require_role("analyst"))) -> dict:
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    request_cancel(str(run_id))
    return {"run_id": str(run_id), "status": "cancelling"}


@router.post("/{run_id}/resume")
async def resume_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(require_role("analyst"))) -> dict:
    """Re-enqueue a failed/interrupted run; the worker resumes from the last checkpoint."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("failed", "cancelled"):
        raise HTTPException(409, "Only failed or cancelled runs can be resumed")
    run.status = "queued"
    run.error = None
    await db.commit()

    from worker import enqueue_run

    enqueue_run(str(run_id))
    return {"run_id": str(run_id), "status": "queued"}


@router.post("/{run_id}/review")
async def mark_reviewed(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(require_role("analyst"))) -> dict:
    """Human-approval gate (§10): mark a finished run reviewed and approved."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    run.reviewed = True
    # A needs_review run that a human has reviewed is treated as approved/done.
    if run.status == "needs_review":
        run.status = "done"
    await db.commit()
    return {"run_id": str(run_id), "reviewed": True, "status": run.status}


# --------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------
@router.get("/{run_id}/raw")
async def get_raw(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(current_user)) -> dict:
    return await _report_json(db, run_id, "raw")


@router.get("/{run_id}/final")
async def get_final(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(current_user)) -> dict:
    return await _report_json(db, run_id, "final")


@router.get("/{run_id}/trace")
async def get_trace(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), _user=Depends(current_user)) -> dict:
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {"trace_url": run.langfuse_trace_id or get_settings().langfuse_host}


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------
@router.get("/{run_id}/export")
async def export_report(
    run_id: uuid.UUID,
    format: str = Query(..., pattern="^(pdf|docx)$"),
    report: str = Query("final", pattern="^(raw|final)$"),
    db: AsyncSession = Depends(get_db),
    _user=Depends(current_user),
):
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("done", "needs_review"):
        raise HTTPException(409, "Export available only after the run is finished")

    report_row = (await db.execute(
        select(Report).where(Report.run_id == run_id, Report.kind == report)
    )).scalar_one_or_none()
    if not report_row:
        raise HTTPException(404, "Report not found")

    # Eagerly read the subject before any sync renderer work; accessing a lazy-loaded
    # ORM attribute after a long-running sync call can raise SQLAlchemy's MissingGreenlet
    # because the session may try to refresh the object outside the async greenlet.
    run_subject = run.subject

    from exporters.docx import render_docx
    from exporters.pdf import render_pdf
    from app.core.storage import store_bytes

    # Renderers are CPU-heavy and synchronous (WeasyPrint / python-docx). Run them in a
    # thread pool so the async event loop and DB connection stay alive.
    if format == "pdf":
        data = await asyncio.to_thread(render_pdf, report_row.report_json, report)
        media = "application/pdf"
    else:
        data = await asyncio.to_thread(render_docx, report_row.report_json, report)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # Archive the export (best-effort): a storage failure (e.g. unwritable EXPORT_STORAGE_URI)
    # must NOT block the user's download.
    rel = f"{run_id}/{report}.{format}"
    try:
        uri = store_bytes(rel, data)
        db.add(Export(run_id=run_id, kind=report, format=format, storage_uri=uri))
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    filename = f"{run_subject[:40].replace('/', '-')}-{report}.{format}"
    # RFC 5987 encoding lets browsers handle non-ASCII filenames (e.g., Hebrew) without
    # blowing up the HTTP header's latin-1 encoding.
    encoded = quote(filename, safe="")
    return StreamingResponse(
        iter([data]), media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


# --------------------------------------------------------------------------------------
# SSE stream
# --------------------------------------------------------------------------------------
@router.get("/{run_id}/stream")
async def stream(run_id: uuid.UUID, token: str = Query(default="")):
    # EventSource cannot send Authorization headers, so the token arrives as a query param.
    from app.core.security import decode_token

    if not decode_token(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")

    async def event_gen():
        async for ev in subscribe_events(str(run_id)):
            import json as _json

            yield {"event": ev.get("node", "progress"), "data": _json.dumps(ev)}

    from sse_starlette.sse import EventSourceResponse

    return EventSourceResponse(event_gen())


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
async def _report_json(db: AsyncSession, run_id: uuid.UUID, kind: str) -> dict:
    row = (await db.execute(
        select(Report).where(Report.run_id == run_id, Report.kind == kind)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, f"{kind} report not found")
    return row.report_json


async def _coverage_for(db: AsyncSession, run_id: uuid.UUID) -> Optional[float]:
    row = (await db.execute(
        select(Report.verification).where(Report.run_id == run_id, Report.kind == "final")
    )).scalar_one_or_none()
    if row and isinstance(row, dict):
        return row.get("citation_coverage")
    return None


def _run_summary(run: Run, coverage: Optional[float]) -> dict:
    return {
        "id": str(run.id),
        "subject": run.subject,
        "subject_type": run.subject_type,
        "status": run.status,
        "model": (run.model_config_json or {}).get("global_default"),
        "cost_usd": run.cost_usd,
        "reviewed": run.reviewed,
        "citation_coverage": coverage,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "langfuse_trace_id": run.langfuse_trace_id,
        "error": run.error,
    }
