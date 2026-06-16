"""Single-source prompt assembly (§4.6).

Role and goal are *data* in the plan/config; they only become behaviour when assembled
into a system prompt at dispatch time. We define each template ONCE plus a build
function, so every subagent in the swarm is constructed identically. The citation
discipline and the structured-output contract that the verifier depends on are part of
the fixed/shared block and are therefore identical for every agent.

All templates are also registered in the Langfuse prompt registry (when configured) so
they are versioned and editable without a redeploy; the build functions pull the active
version at runtime and fall back to the local constants if Langfuse is unavailable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.schemas.contracts import AgentSpec


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------
RESEARCH_AGENT_SYSTEM = """\
You are a {role} conducting due-diligence research on:
  Subject: {subject}  ({subject_type})

Goal:
  {goal}

Tools available: {tools}. You may run up to {max_iterations} tool cycles.

Rules (non-negotiable):
- Use only the provided tools; research only publicly available sources.
- For every factual claim, record the exact source URL you took it from.
- Do not infer, embellish, or assert anything a source does not support.
- Label any estimate or unverified item explicitly.

Return BOTH, as a single JSON object:
  "narrative_markdown": full, unedited account of findings (for the RAW report)
  "findings": [ {{ "claim": str, "source_urls": [str],
                   "confidence": "high|medium|low", "category": str|null }} ]
"""

ORCHESTRATOR_SYSTEM = """\
You are the orchestrator/planner for a due-diligence research platform. Decompose the
task into a parallel swarm of research agents plus a consolidation agent.

Subject: {subject}  ({subject_type})
Task: {task}

Produce a WorkflowPlan as a single JSON object with this exact shape:
  {{
    "task": str, "summary": str, "execution_notes": str,
    "agents": [ {{ "name": str, "role": str, "goal": str, "rationale": str,
                   "depends_on": [str], "max_iterations": int,
                   "suggested_tools": ["web_search","scraper"],
                   "model": str|null, "provider": "anthropic" }} ]
  }}

Rules:
- Cover every required FINAL section for the subject type.
- Research agents use web_search + scraper; the consolidation agent uses code_executor.
- Keep the swarm to at most {max_subagents} research agents.
- `depends_on` references other agents' names; no cycles.
"""

AGGREGATOR_SYSTEM = """\
You are the Risk/Profile Consolidation Analyst. You are given the deduplicated findings
of the research swarm for:
  Subject: {subject}  ({subject_type})

Consolidate and deduplicate findings. {bucketing_instruction}
Preserve every source_id; never invent new claims or sources. Assign a severity
(high/medium/low) to each risk bucket for companies.

Return a single JSON object:
  {{ "buckets": [ {{ "category": str, "severity": "high|medium|low|null",
                     "finding_indexes": [int] }} ],
     "notes": str }}
"""

SYNTHESIZER_SYSTEM = """\
You are the report synthesizer (writer). Draft the FINAL report for:
  Subject: {subject}  ({subject_type})
Task: {task}

You are given consolidated findings and a GLOBALLY NUMBERED source list. Write the
required sections for this subject type. For EVERY sourced statement, append one or more
[n] markers referencing the global source ids. Never write a [n] that is not in the
provided source list. A claim with no verifiable source must be dropped or explicitly
marked [unverified] / [estimate] with the basis stated. Net-worth and financial figures
must be sourced or labelled estimates with the basis stated.

{revision_note}

Return a single JSON object:
  {{ "sections": [ {{ "id": str, "title": str, "body_markdown": str,
                      "tables": [ {{ "title": str, "columns": [str], "rows": [[str]] }} ],
                      "citations": [int] }} ] }}
"""

VERIFIER_SYSTEM = """\
You are the citation verifier (LLM-as-judge). You are given numbered claims (each labelled
"CLAIM <index>") together with the stored text of every cited source. For each claim decide
whether the cited source text SUPPORTS it.

Be strict: if the source text does not clearly support the claim, mark it unsupported.
Treat figures, dates and named entities literally.

Return a single JSON object whose "results" has ONE entry per claim index you were given,
referencing the claim by its integer index:
  {{ "results": [ {{ "claim_index": int, "supported": bool, "reason": str }} ] }}
"""


# --------------------------------------------------------------------------------------
# Langfuse prompt registry integration
# --------------------------------------------------------------------------------------
_LOCAL_TEMPLATES = {
    "research_agent_system": RESEARCH_AGENT_SYSTEM,
    "orchestrator_system": ORCHESTRATOR_SYSTEM,
    "aggregator_system": AGGREGATOR_SYSTEM,
    "synthesizer_system": SYNTHESIZER_SYSTEM,
    "verifier_system": VERIFIER_SYSTEM,
}


def get_template(name: str) -> str:
    """Pull the active template version from the Langfuse prompt registry, falling back
    to the local constant if Langfuse is not configured or the prompt is absent."""
    try:
        from langfuse import get_client

        client = get_client()
        prompt = client.get_prompt(name, type="text")
        compiled = prompt.get_langchain_prompt() if hasattr(prompt, "get_langchain_prompt") else prompt.prompt
        if isinstance(compiled, str) and compiled.strip():
            return compiled
    except Exception:
        pass
    return _LOCAL_TEMPLATES[name]


def register_templates() -> None:
    """Register/refresh all local templates in the Langfuse prompt registry so they are
    versioned and editable without a redeploy. Creates the prompt if absent, and pushes a
    new production version when the local canonical text has changed (so code-side edits to
    the templates propagate). Safe no-op if Langfuse is unavailable."""
    try:
        from langfuse import get_client

        client = get_client()
        for name, body in _LOCAL_TEMPLATES.items():
            try:
                existing = client.get_prompt(name, type="text", cache_ttl_seconds=0)
                current = getattr(existing, "prompt", None)
                if isinstance(current, str) and current.strip() == body.strip():
                    continue  # up to date
            except Exception:
                pass  # absent — create below
            client.create_prompt(name=name, prompt=body, type="text", labels=["production"])
    except Exception:
        # Langfuse not configured — local constants are used.
        pass


# --------------------------------------------------------------------------------------
# Build functions (one per role)
# --------------------------------------------------------------------------------------
def build_research_prompt(agent: "AgentSpec", subject: str, subject_type: str) -> str:
    return get_template("research_agent_system").format(
        role=agent.role,
        goal=agent.goal,
        subject=subject,
        subject_type=subject_type,
        tools=", ".join(agent.suggested_tools),
        max_iterations=agent.max_iterations,
    )


def build_orchestrator_prompt(subject: str, subject_type: str, task: str, max_subagents: int) -> str:
    return get_template("orchestrator_system").format(
        subject=subject, subject_type=subject_type, task=task, max_subagents=max_subagents
    )


def build_aggregator_prompt(subject: str, subject_type: str) -> str:
    if subject_type == "company":
        bucketing = (
            "Bucket risk findings into: Regulatory & Compliance, Legal & Litigation, "
            "Sanctions/AML/Corruption, Reputational & Media, ESG/Environmental/Community, "
            "Procurement/Tendering/Counterparty, Jurisdictional Risk, PEP/Political exposure."
        )
    else:
        bucketing = "Bucket findings into: bio, career, investments, financial-legal."
    return get_template("aggregator_system").format(
        subject=subject, subject_type=subject_type, bucketing_instruction=bucketing
    )


def build_synthesizer_prompt(
    subject: str, subject_type: str, task: str, revision_feedback: Optional[str] = None
) -> str:
    revision_note = (
        f"REVISION REQUESTED. Fix these unsupported citations from the previous draft:\n{revision_feedback}"
        if revision_feedback
        else ""
    )
    return get_template("synthesizer_system").format(
        subject=subject, subject_type=subject_type, task=task, revision_note=revision_note
    )


def build_verifier_prompt() -> str:
    return get_template("verifier_system")
