"""synthesizer (writer) node (§4.1 node 4).

Given merged findings + a globally numbered source list, drafts the FINAL report's
required sections (§4.4) citing [n] inline. On a verifier-requested revision, the prior
unsupported-citation feedback is threaded into the prompt.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.core.prompts import build_synthesizer_prompt
from workflow.llm import extract_list, invoke_json
from workflow.models import resolve_model

# Required FINAL sections per subject type (§4.4).
# Company reports are framed as US-COMPLIANCE adverse due diligence: lead with material
# derogatory risk, keep the subject/ownership overview brief, and make no investment
# recommendation. Financials/operations appear only as light context in the overview.
COMPANY_SECTIONS = [
    ("company_overview", "Company Overview"),
    ("company_ownership", "Company Ownership"),
    ("risk_issues", "Risk Issues"),
    ("pep_status", "PEP Status"),
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

    for _ in range(2):  # initial attempt + one retry
        result = invoke_json(writer_model, sys, shared + instruction,
                             callbacks=callbacks, max_tokens=max_tokens)
        cost += result["cost_usd"]
        raw = extract_list(result["data"], "sections")
        match = next((s for s in raw if isinstance(s, dict) and (s.get("body_markdown") or "").strip()), None)
        if match:
            return _normalize_one(match, sid, title), cost
    return _normalize_one({}, sid, title), cost  # empty placeholder; caller decides if fatal



def _company_section_note(sid: str) -> str:
    """Per-section drafting guidance for the 4-section Davis-approved structure."""
    _CITE_RULE = (
        "\n\nCITATION RULE (mandatory): EVERY factual sentence MUST end with one or more "
        "[n] citation markers from the global source list. Sentences without citations WILL "
        "BE FLAGGED as failures. If you cannot cite a claim, either drop it or mark it "
        "[unverified]. Prefer citing sources marked [HAS TEXT] over [NO TEXT]. "
        "Only cite a source if the FINDING it maps to actually supports the specific claim — "
        "do NOT cite loosely related sources."
    )
    notes = {
        "company_overview": (
            " Write a BRIEF factual description (50-100 words) of the company:\n"
            "- What is this company? What do they do (business lines)?\n"
            "- Size (revenue, employees, if available)\n"
            "- Geographic scope (where they operate, key jurisdictions)\n"
            "- Stock listings if any\n"
            "Keep it SHORT and FACTUAL. No analysis, no opinions, no risk assessment here.\n"
            "NEVER cite Wikipedia. Source all facts from SEC filings (Form 20-F, 6-K), "
            "regulatory databases, or official company filings. If a SEC filing source "
            "exists in the source list, use that instead of Wikipedia."
        ),
        "company_ownership": (
            " List ALL known shareholders with ownership percentages. For EACH major shareholder:\n"
            "- Name and percentage\n"
            "- Who controls them (ultimate beneficial owner)\n"
            "- Any PEP connections or politically sensitive associations\n"
            "- Net worth if available\n\n"
            "Format as a structured list with indentation showing control chains, e.g.:\n"
            "```\n"
            "Israel Corporation Ltd.: 43.93%\n"
            "  — Controlled by: Ofer family\n"
            "  — Ultimate beneficial owner: Idan Ofer\n"
            "```\n"
            "End with a brief OWNERSHIP STRUCTURE summary (concentration, governance risk).\n"
            "This section MUST NOT be blank. If ownership data is limited, state what IS known "
            "and note what could not be determined.\n"
            "Do NOT use recommendation language ('should verify', 'recommend')."
        ),
        "risk_issues": (
            " Organize ALL risk findings into THREE subsections with these EXACT headers:\n\n"
            "### CONFIRMED RISKS [CONFIRMED]\n"
            "Facts verified from government databases, court records, regulatory databases, "
            "or official government publications WHERE source text was retrieved.\n\n"
            "### REPORTED ALLEGATIONS [REPORTED]\n"
            "Claims from credible journalists, NGOs (HRW, Amnesty), or advocacy organizations. "
            "Also: SEC filing self-disclosures of adverse risk factors. "
            "Format: '[REPORTED — source name]' e.g. '[REPORTED — Human Rights Watch]'\n\n"
            "### UNVERIFIED ITEMS [UNVERIFIED]\n"
            "Claims where source text was NOT retrieved, or single-source claims lacking "
            "corroboration. For each, note WHY it is unverified.\n\n"
            "PRIORITY ORDERING within each subsection:\n"
            "1. Weapons, Military Supply Chain & Dual-Use Products (white phosphorus, military "
            "   contracts, arms supply chains) — this is the HIGHEST PRIORITY risk category\n"
            "2. Sanctions / Export Controls\n"
            "3. Legal & Litigation (court rulings, fines, criminal investigations)\n"
            "4. Environmental violations\n"
            "5. All other risk categories\n\n"
            "TAGGING RULES:\n"
            "- HRW, Amnesty International findings → [REPORTED — HRW] NOT [CONFIRMED]\n"
            "- ASEED, AFSC, Who Profits findings → [REPORTED — source name, advocacy org]\n"
            "- Credible journalism → [REPORTED — source name]\n"
            "- ICL/company SEC filing self-disclosure → [REPORTED — self-disclosure]\n"
            "- Government/regulatory source with retrieved text → [CONFIRMED]\n"
            "- Source text not retrieved → [UNVERIFIED]\n"
            "- Sanctions screening NEGATIVE results (subject NOT found on OFAC/BIS/UN/EU lists) "
            "→ [CONFIRMED] — these are verified negative results from government databases\n\n"
            "CATEGORIZATION RULES:\n"
            "- Criminal investigations (Green Police, police, prosecution) → Legal & Litigation, "
            "NOT Weapons/Military\n"
            "- Court rulings, fines, settlements → Legal & Litigation\n"
            "- White phosphorus supply contracts, military supply chains → Weapons/Military\n"
            "- Environmental spills, emissions violations → Environmental\n\n"
            "STYLE RULES:\n"
            "- Use bullet points, be CONCISE and DIRECT\n"
            "- State facts, NOT advice. NEVER use 'should', 'recommend', 'warrants investigation'\n"
            "- Use concise supply-chain notation (e.g. 'Bayer → ICL → US Army → Israeli military')\n"
            "- Include ALL findings from the findings list — do NOT omit any\n"
            "- Cover ALL risk subcategories where findings exist: "
            + ", ".join(COMPANY_RISK_SUBCATEGORIES)
        ),
        "pep_status": (
            " List ALL identified Politically Exposed Persons in the ownership, management, "
            "or family connections. For EACH PEP:\n"
            "- **Name**\n"
            "- **Status** (e.g., 'Ultimate beneficial owner', 'Significant influence via X')\n"
            "- **Role** (their position/relationship)\n"
            "- **PEP Level** (CRITICAL / HIGH / MEDIUM / LOW)\n"
            "- **Sanctions designations** (OFAC SDN, BIS, UN, EU — state NONE if none found)\n"
            "- **Net worth** (if available, with source)\n"
            "- **Government connections** (specific relationships)\n"
            "- **Additional notes** (investigations, controversies)\n\n"
            "End with: OVERALL PEP RISK ASSESSMENT: HIGH/MEDIUM-HIGH/MEDIUM/LOW with brief rationale.\n"
            "If NO PEPs identified, state that clearly.\n"
            "Do NOT use recommendation language. State facts only."
        ),
    }
    return notes.get(sid, "") + _CITE_RULE


def _build_shared_context(state: Dict[str, Any]) -> str:
    from workflow.quality.source_tiers import SourceTier, classify_source_tier

    findings = state.get("aggregated_findings", [])
    sources = state.get("sources", [])
    lines: List[str] = []

    # 1. Findings segmented by verification status for the writer.
    sorted_findings = sorted(findings, key=_finding_priority, reverse=True)

    # Map internal finding_type to Davis report tags.
    # CONFIRMED requires: (1) government/regulatory source, (2) source text retrieved.
    # NGO/advocacy/journalism sources are always REPORTED even if claim has fact patterns.
    sources_by_id = {s["id"]: s for s in sources}

    def _report_tag(f: Dict[str, Any]) -> str:
        ft = f.get("finding_type", "analysis")
        source_ids = f.get("source_ids", [])

        # Check source quality for CONFIRMED eligibility.
        has_gov_source_with_text = False
        has_sec_source = False
        all_sources_no_text = True
        for sid in source_ids:
            src = sources_by_id.get(sid, {})
            url = src.get("url", "")
            has_text = bool((src.get("content") or "").strip())
            if has_text:
                all_sources_no_text = False
            tier = classify_source_tier(url)
            if tier == SourceTier.TIER_1 and has_text:
                has_gov_source_with_text = True
            if "sec.gov" in url.lower() or "edgar" in url.lower():
                has_sec_source = True

        # No text retrieved at all → UNVERIFIED regardless.
        if all_sources_no_text and source_ids:
            return "UNVERIFIED"
        # FACT with government source + text → CONFIRMED
        if ft == "fact" and (has_gov_source_with_text or has_sec_source):
            return "CONFIRMED"
        # FACT but only NGO/advocacy/journalism sources → REPORTED
        if ft == "fact":
            return "REPORTED"
        # Analysis → REPORTED
        if ft == "analysis":
            return "REPORTED"
        # Interpretation/advocacy → UNVERIFIED
        return "UNVERIFIED"

    tag_groups: Dict[str, List] = {"CONFIRMED": [], "REPORTED": [], "UNVERIFIED": []}
    for f in sorted_findings[:80]:
        tag = _report_tag(f)
        tag_groups[tag].append(f)

    lines.append("CONSOLIDATED FINDINGS — classified by verification status.")
    lines.append("Each finding is tagged [CONFIRMED], [REPORTED], or [UNVERIFIED].")
    lines.append("Use these tags when writing the Risk Issues section.\n")

    idx = 0
    for tag in ("CONFIRMED", "REPORTED", "UNVERIFIED"):
        group = tag_groups.get(tag, [])
        if not group:
            continue
        lines.append(f"--- [{tag}] FINDINGS ({len(group)}) ---")
        for f in group:
            cat = f.get("category") or "uncategorized"
            conf = f.get("confidence_assessment", {})
            conf_label = conf.get("level", f.get("confidence", "medium")) if conf else f.get("confidence", "medium")
            cd = f.get("circular_dep", {})
            cd_tag = " [CIRCULAR DEP]" if cd.get("has_circular_dep") else ""
            lines.append(
                f"[F:{idx}] [{tag}] ({cat}, confidence={conf_label}{cd_tag}) "
                f"{f['claim']}  -> {f.get('source_ids')}"
            )
            idx += 1
        lines.append("")

    total_omitted = len(sorted_findings) - min(len(sorted_findings), 80)
    if total_omitted > 0:
        lines.append(f"- ... ({total_omitted} additional findings omitted for brevity)")

    # 1b. Include raw agent narratives so the synthesizer has context for Company Overview
    # and other sections that need info beyond the structured findings.
    raw_outputs = state.get("raw_outputs", [])
    if raw_outputs:
        lines.append("\nRAW AGENT NARRATIVES (use for Company Overview and context):")
        for ao in raw_outputs:
            narrative = (ao.get("narrative_markdown") or "").strip()
            if narrative:
                role = ao.get("role") or ao.get("agent", "unknown")
                # Truncate long narratives to keep context manageable.
                if len(narrative) > 2000:
                    narrative = narrative[:2000] + "\n... [truncated]"
                lines.append(f"\n--- {role} ---")
                lines.append(narrative)
        lines.append("")

    # 1c. Inject prior findings from manual due diligence (config/prior_findings/).
    prior = _load_prior_findings(state.get("subject", ""))
    if prior:
        lines.append("\nPRIOR FINDINGS FROM MANUAL DUE DILIGENCE:")
        lines.append("These findings are from prior manual investigations and MUST be included "
                     "in the report. They do NOT have [n] source citations — instead, cite the "
                     "source description provided. Include them in the appropriate sections "
                     "(Risk Issues, Company Ownership, PEP Status) with their given tags.")
        for pf in prior:
            lines.append(f"- [{pf['tag']}] ({pf['category']}) {pf['claim']}")
            lines.append(f"  Source: {pf['source_description']}")
        lines.append("")

    # 2. Global source list — flag retrievability and tier for the writer.
    lines.append("\nGLOBAL SOURCE LIST (cite ONLY these ids as [n]):")
    lines.append("NOTE: Sources marked [NO TEXT] had no retrievable content — the verifier "
                 "CANNOT verify claims citing them. STRONGLY prefer citing sources marked "
                 "[HAS TEXT] whenever multiple sources support the same claim.\n"
                 "NEVER cite Wikipedia sources. Use SEC filings or regulatory databases instead.")
    _TIER_LABELS = {
        SourceTier.TIER_1: "GOV/REGULATORY",
        SourceTier.TIER_2: "MAJOR NEWS",
        SourceTier.TIER_3: "NGO/INVESTIGATIVE",
        SourceTier.TIER_4: "ADVOCACY",
    }
    for s in sources:
        has_content = bool((s.get("content") or "").strip())
        text_tag = "[HAS TEXT]" if has_content else "[NO TEXT]"
        url = s.get("url", "")
        tier = classify_source_tier(url)
        tier_label = _TIER_LABELS.get(tier, "OTHER")
        # Flag Wikipedia and SEC filings explicitly.
        if "wikipedia.org" in url.lower():
            tier_label = "WIKIPEDIA — DO NOT CITE"
        elif "sec.gov" in url.lower() or "edgar" in url.lower():
            tier_label = "SEC FILING"
        lines.append(f"  [{s['id']}] {text_tag} [{tier_label}] {url} — {s.get('title','')}")
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


def _load_prior_findings(subject: str) -> List[Dict[str, Any]]:
    """Load prior manual DD findings from config/prior_findings/ YAML files.

    Files contain subject_patterns (matched case-insensitively against the run subject)
    and a list of findings with claim, tag, category, and source_description.
    """
    import pathlib
    import yaml

    prior_dir = pathlib.Path(__file__).resolve().parents[2] / "config" / "prior_findings"
    if not prior_dir.is_dir():
        return []
    results: List[Dict[str, Any]] = []
    subject_lower = subject.lower()
    for f in prior_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text())
            if not isinstance(data, dict):
                continue
            patterns = data.get("subject_patterns", [])
            if not any(p.lower() in subject_lower for p in patterns):
                continue
            for finding in data.get("findings", []):
                if isinstance(finding, dict) and finding.get("claim"):
                    results.append({
                        "claim": finding["claim"],
                        "tag": finding.get("tag", "UNVERIFIED"),
                        "category": finding.get("category", "Uncategorized"),
                        "source_description": finding.get("source_description", "Prior manual due diligence"),
                    })
        except Exception:
            continue
    return results
