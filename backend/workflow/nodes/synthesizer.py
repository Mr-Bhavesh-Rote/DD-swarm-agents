"""synthesizer (writer) node (§4.1 node 4).

Given merged findings + a globally numbered source list, drafts the FINAL report's
required sections (§4.4) citing [n] inline. On a verifier-requested revision, the prior
unsupported-citation feedback is threaded into the prompt.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.prompts import build_synthesizer_prompt
from workflow.llm import extract_list, invoke_json
from workflow.models import resolve_model

# Required FINAL sections per subject type (§4.4).
COMPANY_SECTIONS = [
    ("executive_summary", "Executive Summary"),
    ("ownership_governance", "Ownership & Governance"),
    ("operations_footprint", "Operations Footprint"),
    ("financial_performance", "Financial Performance"),
    ("risk_issues", "Risk Issues"),
    ("investment_considerations", "Investment Considerations"),
]
COMPANY_RISK_SUBCATEGORIES = [
    "Regulatory & Compliance", "Legal & Litigation", "Sanctions/AML/Corruption",
    "Reputational & Media", "ESG/Environmental/Community",
    "Procurement/Tendering/Counterparty", "Jurisdictional Risk", "PEP/Political exposure",
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

    # Revision feedback from a prior verifier failure.
    revision_feedback = None
    prev_verification = state.get("verification") or {}
    if prev_verification.get("flags"):
        revision_feedback = "\n".join(
            f"- [{fl.get('section_id')}] claim '{fl.get('claim','')[:160]}' "
            f"(cites {fl.get('citation_ids')}): {fl.get('reason','')}"
            for fl in prev_verification["flags"]
        )

    sys = build_synthesizer_prompt(subject, subject_type, task, revision_feedback)
    user = _build_user_payload(state, subject_type)

    from app.core.config import get_settings

    result = invoke_json(writer_model, sys, user, callbacks=callbacks,
                         max_tokens=get_settings().synthesizer_max_tokens)
    sections = _normalize_sections(extract_list(result["data"], "sections"), subject_type)

    return {
        "draft_sections": sections,
        "cost_usd": result["cost_usd"],
        "model_summary": {"writer": writer_model},
        "events": [{"node": "synthesizer", "status": "completed",
                    "n_sections": len(sections),
                    "revision": state.get("revision_count", 0)}],
    }


def _build_user_payload(state: Dict[str, Any], subject_type: str) -> str:
    findings = state.get("aggregated_findings", [])
    sources = state.get("sources", [])
    lines: List[str] = []

    # 1. Full per-agent narratives — the raw detail to PRESERVE in the final report.
    raw_outputs = state.get("raw_outputs", [])
    if raw_outputs:
        lines.append("RAW PER-AGENT RESEARCH (preserve all material detail from these):")
        for ao in raw_outputs:
            lines.append(f"\n### {ao.get('role') or ao.get('agent','')}")
            lines.append(ao.get("narrative_markdown", "") or "")

    # 2. Deduped findings with their resolved citation ids.
    lines.append("\nCONSOLIDATED FINDINGS (claim -> source ids):")
    for f in findings:
        cat = f.get("category") or "uncategorized"
        lines.append(f"- ({cat}, {f.get('confidence')}) {f['claim']}  -> {f.get('source_ids')}")

    # 3. Global source list (id -> url) for citation.
    lines.append("\nGLOBAL SOURCE LIST (cite ONLY these ids as [n]):")
    for s in sources:
        lines.append(f"  [{s['id']}] {s['url']} — {s.get('title','')}")

    secs = required_sections(subject_type)
    lines.append("\nREQUIRED SECTIONS (use these exact ids/titles):")
    for sid, title in secs:
        lines.append(f"  {sid}: {title}")
    if subject_type == "company":
        lines.append("\nWithin 'risk_issues', cover these subcategories: " + ", ".join(COMPANY_RISK_SUBCATEGORIES))
    return "\n".join(lines)


def _normalize_sections(raw: List[Dict[str, Any]], subject_type: str) -> List[Dict[str, Any]]:
    by_id = {s.get("id"): s for s in raw if isinstance(s, dict)}
    out: List[Dict[str, Any]] = []
    for sid, title in required_sections(subject_type):
        s = by_id.get(sid, {})
        out.append({
            "id": sid,
            "title": s.get("title", title),
            "body_markdown": s.get("body_markdown", ""),
            "tables": s.get("tables", []) or [],
            "citations": [c for c in s.get("citations", []) if isinstance(c, int)],
        })
    return out
