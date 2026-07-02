"""research_agent node + fan-out dispatcher (§4.1 node 2).

`dispatch_research` returns a list of `Send` commands — one per research agent — so the
swarm runs in parallel. Each branch runs a tool-calling loop (search -> scrape -> extract)
up to max_iterations, then returns a narrative markdown block and a structured findings
list. Branches only append to reducer-merged channels (findings, raw_outputs, sources_raw).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Send

from app.core.prompts import build_research_prompt
from app.schemas.contracts import AgentSpec
from workflow.llm import _extract_json, _stringify, estimate_cost
from workflow.models import make_chat_model, resolve_model
from workflow.tools import ToolContext, get_tool_fns


# Compliance sources that must be queried by each adverse-research DOMAIN. The gate below
# forces up to 2 extra turns if a required tool was not attempted. This is keyed by domain,
# not by agent name, so it works for both template-mode (fixed names) and AI-tailored mode
# (dynamic names) as long as the agent has the correct domain tag.
REQUIRED_TOOLS_BY_DOMAIN = {
    "sanctions_legal": [
        "ofac_sdn_search", "ofac_nonsdn_search", "bis_entity_list_search",
        "un_sanctions_search", "eu_sanctions_search", "pacer_search",
    ],
    "adverse_conduct": [
        "fpds_search", "usaspending_search", "occrp_search", "who_profits_search",
    ],
    "adverse_media_esg": [
        "epa_echo_search", "osha_search", "violation_tracker_search",
    ],
    "pep_ownership_risk": [
        "ofac_sdn_search", "pacer_search", "who_profits_search",
    ],
}


def dispatch_research(state: Dict[str, Any]) -> List[Send]:
    """Conditional edge: fan out one branch per research agent in the plan.

    The swarm is hard-capped at MAX_SUBAGENTS (§10 cost guardrail) regardless of plan size.
    """
    from app.core.config import get_settings

    cap = get_settings().max_subagents
    plan = state.get("plan", {})
    agents = plan.get("agents", [])
    sends: List[Send] = []
    for a in agents:
        tools = a.get("suggested_tools", [])
        if any(t in tools for t in ("web_search", "scraper", "scrape_url")):
            sends.append(Send("research_agent", {"agent_spec": a, **_branch_inputs(state)}))
        if len(sends) >= cap:
            break
    return sends


def _branch_inputs(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": state.get("run_id", ""),   # needed so the branch can emit a live "running" event
        "subject": state["subject"],
        "subject_type": state["subject_type"],
        "model_config": state.get("model_config", {}),
        "uploaded_file_ids": state.get("uploaded_file_ids", []),
    }


def research_agent_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """One parallel research branch. `state` here is the Send payload."""
    spec = AgentSpec(**state["agent_spec"])
    subject = state["subject"]
    subject_type = state["subject_type"]
    model_config = state.get("model_config", {})
    callbacks = (config or {}).get("callbacks")

    model_id = resolve_model(role="research", model_config=model_config, agent_model=spec.model)
    system_prompt = build_research_prompt(spec, subject, subject_type)

    # Emit a live "running" event the moment this agent starts (the node's own completed
    # event only surfaces when it finishes), so the UI shows it active immediately.
    run_id = state.get("run_id")
    if run_id:
        from app.core.events import publish_event

        publish_event(run_id, {"node": "research_agent", "agent": spec.name,
                               "status": "running", "model": model_id, "run_id": run_id})

    from app.core.config import get_settings

    ctx = ToolContext()
    tool_fns = get_tool_fns(spec.suggested_tools, ctx)
    llm = make_chat_model(model_id, temperature=0.0, max_tokens=get_settings().research_max_tokens)

    total_cost = 0.0
    transcript: List[Any] = [SystemMessage(content=system_prompt)]
    transcript.append(HumanMessage(content=_tool_instructions(subject, spec)))

    # Tool-calling loop: the model emits tool requests as JSON; we execute and feed back.
    # A single failed/timed-out LLM call must not kill the whole run — we tolerate a few
    # consecutive errors, then finalize with whatever findings were gathered so far.
    consecutive_errors = 0
    for _ in range(max(1, spec.max_iterations)):
        try:
            resp = llm.invoke(transcript, config={"callbacks": callbacks} if callbacks else None)
        except Exception as e:  # noqa: BLE001 — network/timeout/transient API errors
            consecutive_errors += 1
            if consecutive_errors >= 3:
                return _finalize(
                    spec, model_id,
                    {"narrative_markdown": f"_Research interrupted after repeated errors: {e}_", "findings": []},
                    ctx, total_cost,
                )
            continue
        consecutive_errors = 0
        text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
        usage = getattr(resp, "usage_metadata", None) or {}
        total_cost += estimate_cost(model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        try:
            parsed = _extract_json(text)
        except ValueError:
            transcript.append(HumanMessage(content="Respond ONLY with JSON: either {\"tool\": ...} or the final {\"narrative_markdown\":..., \"findings\":...}."))
            continue

        # Final answer?
        if isinstance(parsed, dict) and ("narrative_markdown" in parsed or "findings" in parsed):
            return _finalize(spec, model_id, parsed, ctx, total_cost)

        # Tool request?
        if isinstance(parsed, dict) and "tool" in parsed:
            tool_name = parsed.get("tool")
            args = parsed.get("args", {}) or {}
            fn = tool_fns.get(tool_name)
            if not fn:
                obs = {"error": f"unknown tool '{tool_name}'. Available: {list(tool_fns)}"}
            else:
                try:
                    obs = fn(**args)
                except TypeError:
                    # Allow positional single-arg convenience.
                    val = next(iter(args.values()), "")
                    obs = fn(val)
            transcript.append(HumanMessage(content="TOOL_RESULT " + json.dumps(obs)[:12000]))
            continue

        transcript.append(HumanMessage(content="Continue researching, then return the final JSON object."))

    # Completion gate: required compliance sources must be attempted before finalization.
    # If missing, give the agent up to 2 extra turns with explicit instructions.
    missing = _missing_required_tools(spec.domain, ctx)
    extra_turns = 0
    while missing and extra_turns < 2:
        extra_turns += 1
        prompt = (
            "REQUIRED SOURCE CHECK: before you finalize, you must query these mandated "
            "compliance databases that have not yet been attempted: " + ", ".join(missing) +
            ". Call each as a tool now, then return the final JSON object."
        )
        transcript.append(HumanMessage(content=prompt))
        try:
            resp = llm.invoke(transcript, config={"callbacks": callbacks} if callbacks else None)
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
            continue
        consecutive_errors = 0
        text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
        usage = getattr(resp, "usage_metadata", None) or {}
        total_cost += estimate_cost(model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        try:
            parsed = _extract_json(text)
        except ValueError:
            transcript.append(HumanMessage(content="Respond ONLY with JSON: either {\"tool\": ...} or the final {\"narrative_markdown\":..., \"findings\":...}."))
            continue
        if isinstance(parsed, dict) and ("narrative_markdown" in parsed or "findings" in parsed):
            return _finalize(spec, model_id, parsed, ctx, total_cost)
        if isinstance(parsed, dict) and "tool" in parsed:
            tool_name = parsed.get("tool")
            args = parsed.get("args", {}) or {}
            fn = tool_fns.get(tool_name)
            if not fn:
                obs = {"error": f"unknown tool '{tool_name}'. Available: {list(tool_fns)}"}
            else:
                try:
                    obs = fn(**args)
                except TypeError:
                    val = next(iter(args.values()), "")
                    obs = fn(val)
            transcript.append(HumanMessage(content="TOOL_RESULT " + json.dumps(obs)[:12000]))
            missing = _missing_required_tools(spec.domain, ctx)
            continue
        transcript.append(HumanMessage(content="Continue querying the missing sources, then return the final JSON object."))
        missing = _missing_required_tools(spec.domain, ctx)

    # Loop exhausted: ask for the final synthesis.
    transcript.append(HumanMessage(content="Iteration budget exhausted. Return the final JSON object now."))
    resp = llm.invoke(transcript, config={"callbacks": callbacks} if callbacks else None)
    text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
    try:
        parsed = _extract_json(text)
    except ValueError:
        parsed = {"narrative_markdown": text, "findings": []}
    return _finalize(spec, model_id, parsed, ctx, total_cost)


import re as _re

# Patterns matching tool-call JSON and TOOL_RESULT blocks that the model sometimes
# leaks into the narrative_markdown instead of keeping them out of the final output.
_TOOL_CALL_RE = _re.compile(
    r'^\s*\{["\s]*tool["\s]*:.*?\}\s*$',
    _re.MULTILINE,
)
_TOOL_RESULT_RE = _re.compile(
    r'TOOL_RESULT\s+.*?(?=\n\n|\n\{|\Z)',
    _re.DOTALL,
)
_JSON_BLOCK_RE = _re.compile(
    r'```(?:json)?\s*\{["\s]*tool["\s]*:.*?```',
    _re.DOTALL,
)


def _clean_narrative(text: str) -> str:
    """Strip tool-call JSON and TOOL_RESULT blocks from the narrative markdown.

    The research agent sometimes includes its tool-calling transcript in the
    narrative_markdown field. This pollutes the raw report and PDF exports with
    unreadable JSON. We strip these patterns while preserving the actual prose.
    """
    text = _JSON_BLOCK_RE.sub("", text)
    text = _TOOL_RESULT_RE.sub("", text)
    text = _TOOL_CALL_RE.sub("", text)
    # Collapse runs of blank lines left by removals.
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _missing_required_tools(domain: str, ctx: ToolContext) -> List[str]:
    """Return the required tools for this domain that have not been recorded in tool_calls."""
    required = REQUIRED_TOOLS_BY_DOMAIN.get(domain, [])
    if not required:
        return []
    called = {c.get("tool") for c in ctx.tool_calls}
    return [r for r in required if r not in called]


def _tool_instructions(subject: str, spec: AgentSpec) -> str:
    required = REQUIRED_TOOLS_BY_DOMAIN.get(spec.domain, [])
    required_note = (
        f"\nREQUIRED: before finishing, you MUST call these compliance-source tools: {', '.join(required)}. "
        f"Use each tool with {{\"tool\": \"<tool_name>\", \"args\": {{\"name\": \"{subject}\"}}}}."
        if required else ""
    )
    return (
        f"Research subject: {subject}\n"
        f"To use a tool, respond with ONLY JSON: {{\"tool\": \"web_search\", \"args\": {{\"query\": \"...\"}}}} "
        f"or {{\"tool\": \"scrape_url\", \"args\": {{\"url\": \"...\"}}}}, or use one of the "
        f"dedicated compliance-source tools assigned to you.\n"
        f"Run a few searches from different angles and scrape only the most relevant pages to gather "
        f"primary-source detail (exact figures, dates, names, filings, quotes). You have up to "
        f"{spec.max_iterations} tool cycles, but STOP EARLY and return your final answer as soon as you "
        f"have enough sourced detail — do not keep searching for its own sake.{required_note}\n"
        f"When done, respond with ONLY the final JSON object containing 'narrative_markdown' and 'findings'.\n"
        f"Make 'narrative_markdown' a thorough, well-structured account with ALL specifics you found "
        f"(use markdown tables for structured data); do not summarize away detail. Record a source URL "
        f"for every factual claim in 'findings'."
    )


def _finalize(spec: AgentSpec, model_id: str, parsed: Any, ctx: ToolContext, cost: float) -> Dict[str, Any]:
    # The model sometimes returns a JSON array instead of the agreed object. Coerce so we
    # never crash with "'list' object has no attribute 'get'".
    if isinstance(parsed, list):
        # If it's a list of finding dicts, treat it as the findings; if it wraps the object,
        # take the first dict; otherwise empty.
        obj = next((x for x in parsed if isinstance(x, dict) and ("narrative_markdown" in x or "findings" in x)), None)
        parsed = obj if obj is not None else {"findings": [x for x in parsed if isinstance(x, dict)]}
    if not isinstance(parsed, dict):
        parsed = {}
    narrative = _clean_narrative(parsed.get("narrative_markdown", "") or "")
    raw_findings = parsed.get("findings", []) or []
    findings: List[Dict[str, Any]] = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        findings.append({
            "agent": spec.name,
            "claim": f.get("claim", ""),
            "source_urls": f.get("source_urls", []) or [],
            "confidence": f.get("confidence", "medium"),
            "category": f.get("category"),
        })

    agent_output = {
        "agent": spec.name,
        "role": spec.role,
        "domain": spec.domain,
        "model": model_id,
        "narrative_markdown": narrative,
        "findings": findings,         # carries source_urls; ids assigned in aggregator
        "tool_calls": ctx.tool_calls,
    }

    return {
        "raw_outputs": [agent_output],
        "findings": findings,
        "sources_raw": ctx.fetched_sources,
        "cost_usd": cost,
        "model_summary": {"research": model_id},
        "events": [{"node": "research_agent", "agent": spec.name, "status": "completed",
                    "model": model_id, "n_findings": len(findings),
                    "n_tool_calls": len(ctx.tool_calls)}],
    }
