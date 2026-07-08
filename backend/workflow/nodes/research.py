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


def dispatch_overview(state: Dict[str, Any]) -> List[Send]:
    """Phase 1: fan-out overview_ownership agents only so their entity discoveries can seed
    the adverse/sanctions agents in phase 2 via entity_extractor_node."""
    plan = state.get("plan", {})
    agents = plan.get("agents", [])
    sends: List[Send] = []
    for a in agents:
        if a.get("domain") == "overview_ownership":
            tools = a.get("suggested_tools", [])
            if any(t in tools for t in ("web_search", "scraper", "scrape_url")):
                sends.append(Send("overview_agent", {"agent_spec": a, **_branch_inputs(state)}))
    if sends:
        return sends
    # No overview agent in plan — skip directly to entity_extractor (which will dispatch adverse).
    return "entity_extractor"


def entity_extractor_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Collect all findings from the overview research phase and build a compact entity
    context string.  This is injected into every adverse/sanctions/pep agent's prompt so
    they chain-search for CIG Companies, Jeff Sheehan, etc. even when those names would
    not be discovered from the subject name alone."""
    overview_outputs = [
        ao for ao in (state.get("raw_outputs") or [])
        if ao.get("domain") == "overview_ownership"
    ]
    context_lines: List[str] = []
    for ao in overview_outputs:
        for f in (ao.get("findings") or []):
            claim = f.get("claim", "")
            if claim:
                context_lines.append(f"- {claim}")
    # Cap to avoid bloating the adverse agents' prompts.
    overview_context = "\n".join(context_lines[:40])
    return {
        "overview_context": overview_context,
        "events": [{"node": "entity_extractor", "status": "completed",
                    "n_entities": len(context_lines)}],
    }


def dispatch_adverse(state: Dict[str, Any]) -> List[Send]:
    """Phase 2: fan-out all non-overview agents, injecting entity context from overview."""
    from app.core.config import get_settings

    cap = get_settings().max_subagents
    plan = state.get("plan", {})
    agents = plan.get("agents", [])
    overview_context = state.get("overview_context", "")
    sends: List[Send] = []
    for a in agents:
        if a.get("domain") == "overview_ownership":
            continue  # already ran in phase 1
        tools = a.get("suggested_tools", [])
        if any(t in tools for t in ("web_search", "scraper", "scrape_url")):
            sends.append(Send("research_agent", {
                "agent_spec": a,
                "overview_context": overview_context,
                **_branch_inputs(state),
            }))
        if len(sends) >= cap:
            break
    return sends


# Keep for backwards-compatibility (AI-tailored mode may call this directly).
def dispatch_research(state: Dict[str, Any]) -> List[Send]:
    """Legacy single-phase fan-out — superseded by dispatch_overview/dispatch_adverse."""
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
    overview_context = state.get("overview_context", "")
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
    transcript.append(HumanMessage(content=_tool_instructions(subject, spec, overview_context)))

    # Tool-calling loop: the model emits tool requests as JSON; we execute and feed back.
    # A single failed/timed-out LLM call must not kill the whole run — we tolerate a few
    # consecutive errors, then finalize with whatever findings were gathered so far.
    consecutive_errors = 0
    zero_findings_rejects = 0  # track how many times we pushed back on empty findings
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

        # Final answer? Accept only when ALL conditions are met:
        # (1) >=3 total tool calls, (2) at least 1 web_search was done (compliance-DB-only
        # calls are not sufficient — they return no results for clean subjects and must be
        # paired with actual web research), (3) findings are non-empty OR we've already
        # pushed back on empty findings twice (genuinely clean subject).
        if isinstance(parsed, dict) and ("narrative_markdown" in parsed or "findings" in parsed):
            raw_findings = parsed.get("findings", []) or []
            enough_calls = len(ctx.tool_calls) >= 3
            web_done = _web_call_count(ctx) >= 1

            if enough_calls and web_done and (raw_findings or zero_findings_rejects >= 2):
                return _finalize(spec, model_id, parsed, ctx, total_cost)
            elif not enough_calls:
                transcript.append(HumanMessage(content=(
                    f"You have only made {len(ctx.tool_calls)} tool call(s). This is not enough research. "
                    f"You MUST make at least 3 more searches before finalizing. Try different search "
                    f"queries — vary keywords, search for specific events, people, or regulatory actions. "
                    f"Do NOT return your final answer yet."
                )))
            elif not web_done:
                transcript.append(HumanMessage(content=(
                    f"You have called compliance databases but no web searches. "
                    f"Compliance databases returning no results does NOT mean there are no findings — "
                    f"you MUST also search the web for court cases, regulatory fines, enforcement "
                    f"actions, and controversies. Search now with: "
                    f"{{\"tool\": \"web_search\", \"args\": {{\"query\": \"{subject} lawsuit fine penalty "
                    f"regulatory enforcement 2023 2024 2025\"}}}}"
                )))
            else:
                # Has >=3 calls + web search but 0 findings — push to extract from results.
                zero_findings_rejects += 1
                transcript.append(HumanMessage(content=(
                    f"You returned 0 findings, but your search results above contain relevant "
                    f"information. Review EVERY search result in this transcript carefully and extract "
                    f"specific factual claims with source URLs. Do NOT return 0 findings if any result "
                    f"mentions names, dates, monetary amounts, court rulings, or events about {subject}."
                )))
            continue

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
            # Only accept if we've done at least one web search — compliance tools alone are
            # not sufficient research (they return no results for legitimate companies).
            if _web_call_count(ctx) >= 1:
                return _finalize(spec, model_id, parsed, ctx, total_cost)
            # Otherwise fall through to keep looping / force web searches below.
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

    # Minimum-research gate: if fewer than 3 tool calls OR no web search was done (e.g.
    # agent only called compliance DBs that returned nothing), force web searches in code
    # and give the LLM another pass to extract findings from them.
    if len(ctx.tool_calls) < 3 or _web_call_count(ctx) == 0:
        domain = spec.domain or ""
        if domain == "sanctions_legal":
            _default_queries = [
                f"{subject} lawsuit settlement fine penalty court ruling",
                f"{subject} sanctions enforcement investigation regulatory action",
                f"{subject} litigation criminal case judgment",
            ]
        elif domain == "adverse_conduct":
            _default_queries = [
                f"{subject} corruption bribery fraud investigation FCPA",
                f"{subject} human rights labor abuse scandal controversy",
                f"{subject} weapons military dual-use supply chain",
            ]
        elif domain == "adverse_media_esg":
            _default_queries = [
                f"{subject} environmental fine penalty pollution violation",
                f"{subject} adverse media controversy scandal investigation",
                f"{subject} workplace safety health death fatality",
            ]
        else:
            _default_queries = [
                f"{subject} major shareholders ownership percentage",
                f"{subject} controversies scandal investigation",
                f"{subject} regulatory fine penalty enforcement",
            ]
        web_search_fn = tool_fns.get("web_search")
        if web_search_fn:
            for q in _default_queries:
                if len(ctx.tool_calls) >= 3 and _web_call_count(ctx) >= 1:
                    break
                try:
                    obs = web_search_fn(query=q)
                    transcript.append(HumanMessage(
                        content=f"AUTO-SEARCH for '{q}':\nTOOL_RESULT " + json.dumps(obs)[:12000]
                    ))
                except Exception:
                    pass

        # Give the LLM a turn to process the auto-search results and make more tool calls
        # (e.g. scrape relevant URLs found in search results).
        for _ in range(3):
            transcript.append(HumanMessage(content=(
                "I've run additional searches above. Review ALL search results carefully. "
                "If you see relevant URLs, scrape them with {\"tool\": \"scrape_url\", \"args\": {\"url\": \"...\"}}. "
                "Otherwise, return your final JSON with narrative_markdown and findings. "
                "You MUST extract findings from ALL search results above — do NOT return 0 findings "
                "if the search results contain relevant information about " + subject + "."
            )))
            try:
                resp = llm.invoke(transcript, config={"callbacks": callbacks} if callbacks else None)
            except Exception:
                break
            text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
            usage = getattr(resp, "usage_metadata", None) or {}
            total_cost += estimate_cost(model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            try:
                parsed = _extract_json(text)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "tool" in parsed:
                tool_name = parsed.get("tool")
                args = parsed.get("args", {}) or {}
                fn = tool_fns.get(tool_name)
                if fn:
                    try:
                        obs = fn(**args)
                    except TypeError:
                        val = next(iter(args.values()), "")
                        obs = fn(val)
                    transcript.append(HumanMessage(content="TOOL_RESULT " + json.dumps(obs)[:12000]))
                continue
            if isinstance(parsed, dict) and ("narrative_markdown" in parsed or "findings" in parsed):
                return _finalize(spec, model_id, parsed, ctx, total_cost)

    # Loop exhausted: ask for the final synthesis.
    transcript.append(HumanMessage(content=(
        "Return the final JSON object now with narrative_markdown and findings. "
        "Extract ALL relevant facts from the search results above into findings. "
        "Each finding needs: claim, source_urls, confidence, category. "
        "Do NOT return an empty findings list if search results contained information."
    )))
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


def _web_call_count(ctx: ToolContext) -> int:
    """Count only web_search/scraper calls — compliance-DB calls don't count as web research."""
    web_tools = {"web_search", "scraper", "scrape_url"}
    return sum(1 for c in ctx.tool_calls if c.get("tool") in web_tools)


def _tool_instructions(subject: str, spec: AgentSpec, overview_context: str = "") -> str:
    required = REQUIRED_TOOLS_BY_DOMAIN.get(spec.domain, [])
    required_note = (
        f"\nREQUIRED: before finishing, you MUST call these compliance-source tools: {', '.join(required)}. "
        f"Use each tool with {{\"tool\": \"<tool_name>\", \"args\": {{\"name\": \"{subject}\"}}}}."
        if required else ""
    )
    # Chain-search rule — every agent must follow up on any new entity it discovers.
    chain_search_note = (
        "\nCHAIN-SEARCH RULE: whenever you discover a new person name, company name, or related "
        "entity, IMMEDIATELY run a follow-up search before moving on: "
        "'{name} lawsuit litigation court case', '{name} regulatory fine penalty', "
        "'{name} fraud corruption scandal'. Do NOT skip any lead."
    )
    # Entity context injected from the overview phase — adverse/sanctions/pep agents use this
    # to chain-search for connected persons and companies they wouldn't find from the subject
    # name alone (e.g. CIG Companies → CIG Companies Atlanta hotel litigation).
    entity_note = ""
    if overview_context and spec.domain not in ("overview_ownership",):
        entity_note = (
            f"\n\nKEY ENTITIES FROM OVERVIEW RESEARCH — investigate ALL of these for adverse "
            f"information relevant to your domain:\n{overview_context}\n"
            f"For EVERY person or company mentioned above, run explicit follow-up searches:\n"
            f"  '{{name}} lawsuit litigation court ruling'\n"
            f"  '{{name}} regulatory fine penalty enforcement action'\n"
            f"  '{{name}} fraud corruption scandal controversy'\n"
            f"Do NOT assume a clean record — search each entity individually."
        )
    return (
        f"Research subject: {subject}\n"
        f"To use a tool, respond with ONLY JSON: {{\"tool\": \"web_search\", \"args\": {{\"query\": \"...\"}}}} "
        f"or {{\"tool\": \"scrape_url\", \"args\": {{\"url\": \"...\"}}}}, or use one of the "
        f"dedicated compliance-source tools assigned to you.\n"
        f"Run MULTIPLE searches from different angles and scrape the most relevant pages to gather "
        f"primary-source detail (exact figures, dates, names, filings, quotes). You have up to "
        f"{spec.max_iterations} tool cycles. You MUST use at least 4 different tool calls "
        f"(web searches with different queries, compliance database checks, scraping key pages) "
        f"before returning your final answer. Do NOT return your final answer after only 1-2 searches — "
        f"you will miss critical findings.{required_note}{chain_search_note}{entity_note}\n"
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
