"""synthesizer (writer) node (§4.1 node 4).

Given merged findings + a globally numbered source list, drafts the FINAL report's
required sections (§4.4) citing [n] inline. On a verifier-requested revision, the prior
unsupported-citation feedback is threaded into the prompt.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.prompts import build_synthesizer_prompt
from workflow.llm import extract_list, invoke_json
from workflow.models import resolve_model


class SubjectOverview(BaseModel):
    """Structured, length-capped Subject Overview section.
    Each field ending in _cited should contain inline [n] citation markers."""
    legal_name: str = Field(default="", max_length=200)
    jurisdiction: str = Field(default="", max_length=120)
    business_one_liner: str = Field(default="", max_length=300)
    stock_listing: Optional[str] = Field(default=None, max_length=120)
    ubo_summary: str = Field(default="", max_length=500)
    pep_note: Optional[str] = Field(default=None, max_length=300)
    state_influence_note: Optional[str] = Field(default=None, max_length=300)
    citations: List[int] = Field(default_factory=list)

# Required FINAL sections per subject type (§4.4).
# Company reports are framed as US-COMPLIANCE adverse due diligence: lead with material
# derogatory risk, keep the subject/ownership overview brief, and make no investment
# recommendation. Financials/operations appear only as light context in the overview.
COMPANY_SECTIONS = [
    ("executive_summary", "Executive Summary"),
    ("subject_overview", "Subject Overview & Ownership"),
    ("risk_issues", "Risk Issues"),
    ("compliance_assessment", "Compliance Assessment & Confidence"),
]
# Derogatory/adverse risk categories that form the SPINE of the risk section. These are
# issues affecting the subject (litigation, sanctions, human-rights, controversial/dual-use
# products, corruption, environmental harm) — NOT the subject's own risk-management posture.
COMPANY_RISK_SUBCATEGORIES = [
    "Sanctions / Export Controls / AML",
    "Legal & Litigation (civil, criminal, regulatory enforcement)",
    "Corruption, Bribery & Fraud",
    "Human Rights, Labor & Modern Slavery",
    "Controversial / Dual-Use / Military Products & End-Use",
    "Environmental Harm & ESG Controversies",
    "Regulatory & Compliance Breaches",
    "Reputational & Adverse Media",
    "State Ownership / Political Ties / PEP Exposure",
    "Jurisdictional & Counterparty Risk",
]
INDIVIDUAL_SECTIONS = [
    ("identity_background", "Identity & Background"),
    ("education_career", "Education & Career History"),
    ("current_role", "Current Role/Affiliations"),
    ("investment_portfolio", "Investment & Portfolio History"),
    ("net_worth", "Net Worth (sourced/estimated)"),
    ("board_advisory", "Board & Advisory Positions"),
    ("legal_regulatory", "Legal/Regulatory involvements"),
    ("controversies", "Controversies/Reputational"),
    ("summary_assessment", "Summary Assessment"),
]


def required_sections(subject_type: str) -> List[tuple[str, str]]:
    return COMPANY_SECTIONS if subject_type == "company" else INDIVIDUAL_SECTIONS


def synthesizer_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    subject = state["subject"]
    subject_type = state["subject_type"]
    task = state.get("task", "")
    model_config = state.get("model_config", {})
    callbacks = (config or {}).get("callbacks")

    writer_model = resolve_model(role="writer", model_config=model_config)

    # Revision feedback from a prior verifier failure, grouped by section so we only
    # re-draft the sections that were actually flagged (cheaper than redrafting all).
    prev_verification = state.get("verification") or {}
    flags_by_section: Dict[str, List[Dict[str, Any]]] = {}
    for fl in prev_verification.get("flags", []) or []:
        flags_by_section.setdefault(fl.get("section_id", ""), []).append(fl)
    is_revision = bool(flags_by_section)
    prior_sections = {s.get("id"): s for s in state.get("draft_sections", []) or []}

    shared = _build_shared_context(state)  # narratives + findings + global source list

    # Draft EACH section in its own call. One-shot "all sections in one JSON" used to
    # truncate at the output-token cap on content-rich subjects, producing unparseable JSON
    # that silently degraded to an empty report. Per-section bounds each output.
    sections: List[Dict[str, Any]] = []
    total_cost = 0.0
    for sid, title in required_sections(subject_type):
        section_flags = flags_by_section.get(sid)
        # On a revision pass, keep previously-clean sections untouched.
        if is_revision and not section_flags and sid in prior_sections:
            sections.append(prior_sections[sid])
            continue
        sec, cost = _draft_one_section(
            writer_model, subject, subject_type, task, sid, title, shared,
            section_flags=section_flags, callbacks=callbacks,
        )
        sections.append(sec)
        total_cost += cost

    # (A) Fail loud rather than ship a blank report: if NOTHING came back with content, the
    # synthesis genuinely failed — raise so the run is marked failed (and resumable) instead
    # of persisting an empty "done" report that the verifier would rubber-stamp.
    if not any((s.get("body_markdown") or "").strip() for s in sections):
        raise RuntimeError(
            "Synthesizer produced no content for any section "
            f"({len(sections)} sections, all empty) — likely an LLM/JSON failure."
        )

    # (B) Pre-verify citation coverage: redraft any section below 60% coverage ONCE.
    # This catches the "52% uncited" problem before the verifier even runs, saving a
    # full verifier→revision round-trip for what is essentially a formatting issue.
    import re
    _cite_check = re.compile(r"\[(\d+)\]")
    _sent_check = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
    for i, sec in enumerate(sections):
        body = sec.get("body_markdown", "") or ""
        sentences = [s.strip() for s in _sent_check.split(body) if len(s.strip()) >= 12]
        if not sentences:
            continue
        cited_count = sum(1 for s in sentences if _cite_check.search(s))
        coverage = cited_count / len(sentences)
        if coverage < 0.6:
            # Redraft this section with explicit citation-gap feedback.
            sid, title = sec["id"], sec["title"]
            uncited_examples = [s[:100] for s in sentences if not _cite_check.search(s)][:5]
            gap_feedback = (
                f"CITATION GAP: This section has only {coverage:.0%} citation coverage "
                f"({cited_count}/{len(sentences)} sentences cited). "
                f"Examples of UNCITED sentences that MUST be fixed:\n"
                + "\n".join(f"- \"{ex}...\"" for ex in uncited_examples)
                + "\n\nRewrite the section so that EVERY factual sentence ends with [n] citations. "
                "Drop any claim you cannot cite."
            )
            redraft, redraft_cost = _draft_one_section(
                writer_model, subject, subject_type, task, sid, title, shared,
                section_flags=[{"section_id": sid, "claim": "uncited sentences",
                                "citation_ids": [], "reason": gap_feedback}],
                callbacks=callbacks,
            )
            # Only accept the redraft if it actually improved coverage.
            redraft_body = redraft.get("body_markdown", "") or ""
            redraft_sents = [s.strip() for s in _sent_check.split(redraft_body) if len(s.strip()) >= 12]
            if redraft_sents:
                redraft_cited = sum(1 for s in redraft_sents if _cite_check.search(s))
                if redraft_cited / len(redraft_sents) > coverage:
                    sections[i] = redraft
            total_cost += redraft_cost

    return {
        "draft_sections": sections,
        "cost_usd": total_cost,
        "model_summary": {"writer": writer_model},
        "events": [{"node": "synthesizer", "status": "completed",
                    "n_sections": len(sections),
                    "revision": state.get("revision_count", 0)}],
    }


def _draft_one_section(
    writer_model: str, subject: str, subject_type: str, task: str, sid: str, title: str,
    shared: str, *, section_flags: List[Dict[str, Any]] | None, callbacks: Any,
) -> tuple[Dict[str, Any], float]:
    """Draft a single required section. Retries once on an empty/unparseable result before
    giving up (the caller treats an all-empty report as a hard failure)."""
    from app.core.config import get_settings

    feedback = None
    if section_flags:
        feedback = "\n".join(
            f"- claim '{fl.get('claim','')[:160]}' (cites {fl.get('citation_ids')}): {fl.get('reason','')}"
            for fl in section_flags
        )
    sys = build_synthesizer_prompt(subject, subject_type, task, feedback)

    section_note = _company_section_note(sid) if subject_type == "company" else ""
    instruction = (
        f"\n\nWrite ONLY ONE section now: id='{sid}', title='{title}'.{section_note}\n"
        f"Return a single JSON object: {{ \"sections\": [ {{ \"id\": \"{sid}\", \"title\": \"{title}\", "
        f"\"body_markdown\": str, \"tables\": [...], \"citations\": [int] }} ] }}"
    )
    max_tokens = get_settings().synthesizer_max_tokens

    cost = 0.0
    if sid == "subject_overview":
        return _draft_subject_overview(writer_model, shared, callbacks, max_tokens, title)

    for _ in range(2):  # initial attempt + one retry
        result = invoke_json(writer_model, sys, shared + instruction,
                             callbacks=callbacks, max_tokens=max_tokens)
        cost += result["cost_usd"]
        raw = extract_list(result["data"], "sections")
        match = next((s for s in raw if isinstance(s, dict) and (s.get("body_markdown") or "").strip()), None)
        if match:
            return _normalize_one(match, sid, title), cost
    return _normalize_one({}, sid, title), cost  # empty placeholder; caller decides if fatal


def _draft_subject_overview(
    writer_model: str, shared: str, callbacks: Any, max_tokens: int, title: str
) -> tuple[Dict[str, Any], float]:
    """Draft the Subject Overview as a structured, length-capped section.

    Uses the same prose-based drafting as other sections for format consistency,
    but with strict length/scope constraints via the section note.
    """
    from app.core.config import get_settings

    sys = (
        "You are drafting the Subject Overview & Ownership section of a US-compliance adverse "
        "due-diligence report. Using ONLY the provided findings, write a BRIEF overview.\n\n"
        "RULES:\n"
        "- Keep it to 2-3 SHORT paragraphs maximum.\n"
        "- Cover: legal name, jurisdiction, one-line business description, key ownership/UBO, "
        "and any state/political ties or PEP exposure.\n"
        "- Do NOT add detailed shareholder tables, multi-paragraph ownership chains, financial "
        "statements, or operational deep-dives.\n"
        "- EVERY factual sentence MUST end with [n] citation(s) from the global source list. "
        "Sentences without citations will be flagged as failures.\n"
        "- Prefer sources marked [HAS TEXT] over [NO TEXT].\n"
    )
    instruction = (
        f"\n\nWrite ONLY ONE section now: id='subject_overview', title='{title}'.\n"
        f"Return a single JSON object: {{ \"sections\": [ {{ \"id\": \"subject_overview\", "
        f"\"title\": \"{title}\", \"body_markdown\": str, \"tables\": [], \"citations\": [int] }} ] }}"
    )
    result = invoke_json(writer_model, sys, shared + instruction,
                         callbacks=callbacks, max_tokens=max_tokens)
    cost = result["cost_usd"]
    raw = extract_list(result["data"], "sections")
    match = next((s for s in raw if isinstance(s, dict) and (s.get("body_markdown") or "").strip()), None)
    return _normalize_one(match or {}, "subject_overview", title), cost


def _company_section_note(sid: str) -> str:
    """Per-section drafting guidance that enforces the US-compliance adverse-DD framing.

    Every section note ends with the citation enforcement rule so uncited prose is never
    generated — this is the primary lever for solving the 52% uncited problem.
    """
    _CITE_RULE = (
        "\n\nCITATION RULE (mandatory): EVERY factual sentence MUST end with one or more "
        "[n] citation markers from the global source list. Sentences without citations WILL "
        "BE FLAGGED as failures. If you cannot cite a claim, either drop it or mark it "
        "[unverified]. Prefer citing sources marked [HAS TEXT] over [NO TEXT]. "
        "Only cite a source if the FINDING it maps to actually supports the specific claim — "
        "do NOT cite loosely related sources."
    )
    if sid == "executive_summary":
        return (
            " Open with the MOST MATERIAL derogatory/adverse findings about the subject "
            "(sanctions, litigation, human-rights, controversial products, corruption, etc.). "
            "Two or three tight paragraphs — this is a compliance screening, not an investment memo. "
            "Do NOT include any investment recommendation." + _CITE_RULE
        )
    if sid == "subject_overview":
        return (
            " Keep this BRIEF (a few short paragraphs): what the subject does, corporate structure, "
            "ultimate beneficial owners, key management, and any state/political ties or PEP exposure. "
            "Mention operations and financials ONLY as light context needed to understand the subject — "
            "do NOT produce detailed financial statements, ratios, or operational deep-dives." + _CITE_RULE
        )
    if sid == "risk_issues":
        return (
            " This is the CORE of the report — spend most of the report's detail here. Cover ACTUAL "
            "derogatory issues affecting the subject (not the subject's own risk-management posture). "
            "Organize by these subcategories where supported, omitting any with no findings: "
            + ", ".join(COMPANY_RISK_SUBCATEGORIES)
            + ". For each issue give specifics (who, what, when, jurisdiction, status) and severity."
            + _CITE_RULE
        )
    if sid == "compliance_assessment":
        return (
            " Provide an OVERALL compliance/adverse-risk assessment: a risk rating (e.g. High/Medium/Low) "
            "with rationale, the confidence/reliability of sources, and notable information gaps. "
            "This is a compliance conclusion — do NOT make an investment recommendation." + _CITE_RULE
        )
    return _CITE_RULE


def _build_shared_context(state: Dict[str, Any]) -> str:
    findings = state.get("aggregated_findings", [])
    sources = state.get("sources", [])
    lines: List[str] = []

    # 1. Deduped findings with their resolved citation ids — cap to the most material ones.
    #    We sort by severity so derogatory/high-confidence findings survive the cap.
    sorted_findings = sorted(findings, key=_finding_priority, reverse=True)
    lines.append("CONSOLIDATED FINDINGS (claim -> source ids). Each finding is numbered [F:N] for reference:")
    for i, f in enumerate(sorted_findings[:60]):
        cat = f.get("category") or "uncategorized"
        lines.append(f"[F:{i}] ({cat}, {f.get('confidence')}) {f['claim']}  -> {f.get('source_ids')}")
    if len(sorted_findings) > 60:
        lines.append(f"- ... ({len(sorted_findings) - 60} additional findings omitted for brevity)")

    # 2. Global source list (id -> url) for citation — flag sources with no retrievable
    #    content so the writer prefers citing sources that the verifier can actually check.
    lines.append("\nGLOBAL SOURCE LIST (cite ONLY these ids as [n]):")
    lines.append("NOTE: Sources marked [NO TEXT] had no retrievable content — the verifier "
                 "CANNOT verify claims citing them. STRONGLY prefer citing sources marked "
                 "[HAS TEXT] whenever multiple sources support the same claim.")
    for s in sources:
        has_content = bool((s.get("content") or "").strip())
        tag = "[HAS TEXT]" if has_content else "[NO TEXT]"
        lines.append(f"  [{s['id']}] {tag} {s['url']} — {s.get('title','')}")
    return "\n".join(lines)


def _finding_priority(f: Dict[str, Any]) -> int:
    """Rank findings by severity/confidence so high-value derogatory claims survive the cap."""
    conf = {"high": 30, "medium": 20, "low": 10}.get(f.get("confidence"), 15)
    # Boost priority categories that are central to adverse/compliance screening.
    cat = (f.get("category") or "").lower()
    priority_bonus = 0
    if any(x in cat for x in ("sanctions", "weapons", "military", "dual-use", "corruption", "bribery")):
        priority_bonus = 25
    elif any(x in cat for x in ("litigation", "legal", "human rights", "environmental", "reputational")):
        priority_bonus = 15
    return conf + priority_bonus


def _normalize_one(s: Dict[str, Any], sid: str, title: str) -> Dict[str, Any]:
    return {
        "id": sid,
        "title": s.get("title", title) or title,
        "body_markdown": s.get("body_markdown", "") or "",
        "tables": s.get("tables", []) or [],
        "citations": [c for c in s.get("citations", []) if isinstance(c, int)],
    }


