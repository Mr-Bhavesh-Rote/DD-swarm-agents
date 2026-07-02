"""planner node (§4.1 node 1).

Resolves the run into a WorkflowPlan: load + parameterize the subject-type template if it
exists, else call the orchestrator model to generate a plan in the exact schema. A
plan_override on the state always wins (UI-edited / advanced).
"""
from __future__ import annotations

from typing import Any, Dict

from app.core.config import get_settings
from app.core.prompts import build_orchestrator_prompt
from app.schemas.contracts import AgentSpec
from workflow.config_loader import load_plan_for_subject, normalize_plan
from workflow.llm import invoke_json
from workflow.models import resolve_model


_REQUIRED_DOMAINS_BY_SUBJECT = {
    "company": {
        "overview_ownership",
        "sanctions_legal",
        "adverse_conduct",
        "adverse_media_esg",
        "pep_ownership_risk",
    },
}


def planner_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    settings = get_settings()
    subject = state["subject"]
    subject_type = state["subject_type"]
    task = state.get("task", "")
    model_config = state.get("model_config", {})
    planning_mode = state.get("planning_mode") or "template"
    callbacks = (config or {}).get("callbacks")

    event = {"node": "planner", "status": "started", "subject": subject}

    # 1. Explicit override from the UI / advanced editor.
    if state.get("plan_override"):
        plan = normalize_plan(state["plan_override"])
        return _emit(plan, model_config, is_generated=False, event=event)

    # 2. Template for this subject type — unless the run asked the LLM to tailor the swarm
    #    to its task ("ai" mode), which skips straight to generation below.
    if planning_mode != "ai":
        try:
            plan = load_plan_for_subject(subject_type, task=task)
            return _emit(plan, model_config, is_generated=False, event=event)
        except Exception:
            pass  # fall through to generation

    # 3. Generate via the orchestrator model. A per-run cap (max_research_agents) lets the
    #    user bound the AI swarm for cost; it can only tighten the system MAX_SUBAGENTS.
    requested_cap = state.get("max_research_agents") or settings.max_subagents
    max_agents = min(requested_cap, settings.max_subagents)
    orch_model = resolve_model(role="orchestrator", model_config=model_config)
    sys = build_orchestrator_prompt(subject, subject_type, task, max_agents)
    result = invoke_json(
        orch_model,
        sys,
        f"Subject: {subject}\nSubject type: {subject_type}\nTask: {task}",
        callbacks=callbacks,
        max_tokens=4096,
    )
    plan = normalize_plan(result["data"] or {"agents": []})
    plan = _ensure_domain_coverage(plan, subject, subject_type, max_agents)
    out = _emit(plan, model_config, is_generated=True, event=event)
    out["cost_usd"] = result["cost_usd"]
    out["model_summary"] = {"orchestrator": orch_model}
    return out


def _ensure_domain_coverage(plan, subject: str, subject_type: str, max_agents: int):
    """Inject default agents for any missing required domains in AI-generated plans."""
    required = _REQUIRED_DOMAINS_BY_SUBJECT.get(subject_type, set())
    if not required:
        return plan
    covered = {a.domain for a in plan.agents}
    missing = required - covered
    if not missing:
        return plan

    defaults = {
        "overview_ownership": AgentSpec(
            name="overview_ownership_researcher", role="Subject Overview & Ownership Analyst",
            domain="overview_ownership", goal="Brief subject overview and ownership context.",
            suggested_tools=["web_search", "scraper"], max_iterations=4,
        ),
        "sanctions_legal": AgentSpec(
            name="sanctions_legal_researcher", role="Sanctions, Legal & Regulatory Analyst",
            domain="sanctions_legal", goal="Hunt for sanctions, legal, and regulatory issues.",
            suggested_tools=["web_search", "scraper", "ofac_sdn_search", "ofac_nonsdn_search",
                             "bis_entity_list_search", "un_sanctions_search", "eu_sanctions_search",
                             "pacer_search"], max_iterations=6,
        ),
        "adverse_conduct": AgentSpec(
            name="adverse_conduct_researcher", role="Adverse Conduct & Human-Rights Analyst",
            domain="adverse_conduct", goal="Investigate corruption, human-rights, and dual-use/weapons issues.",
            suggested_tools=["web_search", "scraper", "fpds_search", "usaspending_search",
                             "occrp_search", "who_profits_search"], max_iterations=6,
        ),
        "adverse_media_esg": AgentSpec(
            name="adverse_media_esg_researcher", role="Adverse Media & Environmental Analyst",
            domain="adverse_media_esg", goal="Survey adverse media and ESG/environmental issues.",
            suggested_tools=["web_search", "scraper", "epa_echo_search", "osha_search",
                             "violation_tracker_search"], max_iterations=6,
        ),
        "pep_ownership_risk": AgentSpec(
            name="pep_ownership_risk_researcher", role="PEP & Ownership Risk Analyst",
            domain="pep_ownership_risk", goal="Check PEP exposure and ownership risk.",
            suggested_tools=["web_search", "scraper", "ofac_sdn_search", "pacer_search",
                             "who_profits_search"], max_iterations=4,
        ),
    }
    for domain in sorted(missing):
        if len(plan.agents) >= max_agents:
            break
        plan.agents.append(defaults[domain])
    return plan


def _emit(plan, model_config: Dict[str, Any], *, is_generated: bool, event: Dict[str, Any]) -> Dict[str, Any]:
    plan_dict = plan.model_dump()
    plan_dict["_is_generated"] = is_generated
    # The research swarm = agents that search/scrape. The UI pre-populates a card per
    # research agent (as "pending") so the whole swarm is visible the moment planning ends.
    research_agents = [
        {"name": a.name, "role": a.role, "model": a.model or ""}
        for a in plan.research_agents()
    ]
    return {
        "plan": plan_dict,
        "revision_count": 0,
        "events": [
            {**event, "status": "completed",
             "n_agents": len(plan.agents),
             "agents": [a.name for a in plan.agents],
             "research_agents": research_agents}
        ],
    }
