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

    ctx = ToolContext()
    tool_fns = get_tool_fns(spec.suggested_tools, ctx)
    llm = make_chat_model(model_id, temperature=0.0, max_tokens=4096)

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
            transcript.append(HumanMessage(content="TOOL_RESULT " + json.dumps(obs)[:6000]))
            continue

        transcript.append(HumanMessage(content="Continue researching, then return the final JSON object."))

    # Loop exhausted: ask for the final synthesis.
    transcript.append(HumanMessage(content="Iteration budget exhausted. Return the final JSON object now."))
    resp = llm.invoke(transcript, config={"callbacks": callbacks} if callbacks else None)
    text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
    try:
        parsed = _extract_json(text)
    except ValueError:
        parsed = {"narrative_markdown": text, "findings": []}
    return _finalize(spec, model_id, parsed, ctx, total_cost)


def _tool_instructions(subject: str, spec: AgentSpec) -> str:
    return (
        f"Research subject: {subject}\n"
        f"To use a tool, respond with ONLY JSON: {{\"tool\": \"web_search\", \"args\": {{\"query\": \"...\"}}}} "
        f"or {{\"tool\": \"scrape_url\", \"args\": {{\"url\": \"...\"}}}}.\n"
        f"When done, respond with ONLY the final JSON object containing 'narrative_markdown' and 'findings'.\n"
        f"Start by searching for the most relevant public sources."
    )


def _finalize(spec: AgentSpec, model_id: str, parsed: Dict[str, Any], ctx: ToolContext, cost: float) -> Dict[str, Any]:
    narrative = parsed.get("narrative_markdown", "") or ""
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
