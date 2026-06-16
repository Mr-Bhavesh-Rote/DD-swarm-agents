"""planner node (§4.1 node 1).

Resolves the run into a WorkflowPlan: load + parameterize the subject-type template if it
exists, else call the orchestrator model to generate a plan in the exact schema. A
plan_override on the state always wins (UI-edited / advanced).
"""
from __future__ import annotations

from typing import Any, Dict

from app.core.config import get_settings
from app.core.prompts import build_orchestrator_prompt
from workflow.config_loader import load_plan_for_subject, normalize_plan
from workflow.llm import invoke_json
from workflow.models import resolve_model


def planner_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    settings = get_settings()
    subject = state["subject"]
    subject_type = state["subject_type"]
    task = state.get("task", "")
    model_config = state.get("model_config", {})
    callbacks = (config or {}).get("callbacks")

    event = {"node": "planner", "status": "started", "subject": subject}

    # 1. Explicit override from the UI / advanced editor.
    if state.get("plan_override"):
        plan = normalize_plan(state["plan_override"])
        return _emit(plan, model_config, is_generated=False, event=event)

    # 2. Template for this subject type.
    try:
        plan = load_plan_for_subject(subject_type, task=task)
        return _emit(plan, model_config, is_generated=False, event=event)
    except Exception:
        pass  # fall through to generation

    # 3. Generate via the orchestrator model.
    orch_model = resolve_model(role="orchestrator", model_config=model_config)
    sys = build_orchestrator_prompt(subject, subject_type, task, settings.max_subagents)
    result = invoke_json(
        orch_model,
        sys,
        f"Subject: {subject}\nSubject type: {subject_type}\nTask: {task}",
        callbacks=callbacks,
        max_tokens=4096,
    )
    plan = normalize_plan(result["data"] or {"agents": []})
    out = _emit(plan, model_config, is_generated=True, event=event)
    out["cost_usd"] = result["cost_usd"]
    out["model_summary"] = {"orchestrator": orch_model}
    return out


def _emit(plan, model_config: Dict[str, Any], *, is_generated: bool, event: Dict[str, Any]) -> Dict[str, Any]:
    plan_dict = plan.model_dump()
    plan_dict["_is_generated"] = is_generated
    return {
        "plan": plan_dict,
        "revision_count": 0,
        "events": [
            {**event, "status": "completed",
             "n_agents": len(plan.agents),
             "agents": [a.name for a in plan.agents]}
        ],
    }
