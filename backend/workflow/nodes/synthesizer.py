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
    "Weapons, Military Supply Chain and Dual-Use Products",
    "Legal and Litigation",
    "Corruption, Bribery and FCPA",
    "Human Rights, Labor and Occupied Territory Operations",
    "Environmental Harm and Regulatory Violations",
    "Reputational and Adverse Media",
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

    # (0) Code-level guardrail: if company_overview or company_ownership came back blank,
    # auto-generate a minimal fallback from the findings/narratives so we NEVER ship blank.
    if subject_type == "company":
        for i, sec in enumerate(sections):
            if sec["id"] == "company_overview" and not (sec.get("body_markdown") or "").strip():
                sections[i]["body_markdown"] = _fallback_company_overview(state)
            if sec["id"] == "company_ownership" and not (sec.get("body_markdown") or "").strip():
                sections[i]["body_markdown"] = _fallback_company_ownership(state)

        # (0b) Inject prior must_include findings if the LLM omitted them.
        _inject_missing_prior_findings(sections, subject)

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

    # Inject prior findings relevant to THIS section directly into the instruction
    # so the LLM cannot miss them (shared context alone is unreliable).
    prior_injection = ""
    if subject_type == "company":
        prior = _load_prior_findings(subject)
        relevant = []
        for pf in prior:
            if not pf.get("include_in_sections"):
                # No section restriction → include in risk_issues
                if sid == "risk_issues":
                    relevant.append(pf)
            elif sid in pf.get("include_in_sections", []):
                relevant.append(pf)
            elif sid == "risk_issues" and not pf.get("include_in_sections"):
                relevant.append(pf)
        if relevant:
            prior_injection = "\n\n⚠️ MANDATORY FINDINGS FOR THIS SECTION — YOU MUST INCLUDE THESE:\n"
            for pf in relevant:
                prior_injection += f"\n[{pf['tag']}] ({pf['category']}): {pf['claim']}\n"
                prior_injection += f"Source: {pf['source_description']}\n"
            prior_injection += "\nFAILURE TO INCLUDE THE ABOVE FINDINGS IS A CRITICAL ERROR.\n"

    instruction = (
        f"\n\nWrite ONLY ONE section now: id='{sid}', title='{title}'.{section_note}"
        f"{prior_injection}\n"
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
        "\n\nNO RECOMMENDATIONS (mandatory): NEVER write sentences containing 'should', "
        "'recommend', 'warrants', 'advisable', 'prudent', 'suggest that', 'ought to', "
        "'US counterparty should', 'compliance function should'. "
        "State FACTS ONLY. Replace 'should verify X' with 'X: status unclear' or 'X: not confirmed'. "
        "Replace 'warrants investigation' with 'status unknown' or 'not independently confirmed'."
    )
    notes = {
        "company_overview": (
            " Write a BRIEF factual description (50-100 words MAX) of the company:\n"
            "- Full legal name, jurisdiction of incorporation, stock listing (TASE/NYSE)\n"
            "- Primary business lines (what they make/do)\n"
            "- Approximate size (employees, countries of operation)\n"
            "- Key subsidiaries if relevant\n"
            "ONE short paragraph. Do NOT include: ownership, risk findings, sanctions, "
            "litigation, OFAC results, or any derogatory information — those go in other sections.\n"
            "NEVER cite Wikipedia.\n"
            "Use BOTH the findings list AND the raw agent narratives to gather company facts.\n"
            "THIS SECTION IS MANDATORY AND MUST NEVER BE BLANK. A blank response is a critical failure."
        ),
        "company_ownership": (
            " List shareholders with >5% ownership. Keep it CONCISE (100-200 words MAX).\n"
            "Format: Name: percentage — controlled by [UBO], PEP connections if any.\n"
            "End with one-line ownership structure note.\n"
            "This section MUST NOT be blank. No recommendation language.\n"
            "Use BOTH the findings list AND the raw agent narratives to find ownership data.\n"
            "Include ANY ownership findings from the MANDATORY PRIOR FINDINGS section "
            "(look for items tagged [INCLUDE IN: company_ownership])."
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
            "CITATION ACCURACY RULES:\n"
            "- For sanctions screening results, ONLY cite the actual sanctions database source "
            "(sanctionssearch.ofac.treas.gov, etc.) — do NOT cite news articles about sanctions "
            "enforcement actions against OTHER entities as a source for screening results.\n"
            "- ONLY cite a source [n] if the source is specifically about the SAME entity as the claim. "
            "A source about Russia sanctions is NOT a valid citation for a Kazakhstan company's "
            "OFAC screening result.\n"
            "- If no matching source exists in the source list, do NOT cite — mark as [unverified].\n\n"
            "CATEGORIZATION RULES:\n"
            "- Criminal investigations (Green Police, police, prosecution) → Legal & Litigation, "
            "NOT Weapons/Military\n"
            "- Court rulings, fines, settlements → Legal & Litigation\n"
            "- White phosphorus supply contracts, military supply chains → Weapons/Military\n"
            "- Environmental spills, emissions violations → Environmental\n\n"
            "STYLE RULES:\n"
            "- Use bullet points, be CONCISE and DIRECT — target 400-600 words for this section\n"
            "- State facts, NOT advice. NEVER use 'should', 'recommend', 'warrants investigation'\n"
            "- Use concise supply-chain notation (e.g. 'Bayer → ICL → US Army → Israeli military')\n"
            "- Prioritize the MOST MATERIAL findings — consolidate similar items into single bullets\n"
            "- Each bullet: one finding, one line, with [n] citation at the end\n"
            "- Include ALL MANDATORY PRIOR FINDINGS in the appropriate subsection\n"
            "- Cover risk subcategories where findings exist: "
            + ", ".join(COMPANY_RISK_SUBCATEGORIES)
        ),
        "pep_status": (
            " List identified PEPs CONCISELY (100-200 words MAX). For each:\n"
            "- Name — Role — PEP Level (CRITICAL/HIGH/MEDIUM/LOW)\n"
            "- Sanctions status (NONE if none found)\n"
            "- Key connection (one line)\n\n"
            "Include ANY PEP findings from the MANDATORY PRIOR FINDINGS section.\n\n"
            "End with: OVERALL PEP RISK: HIGH/MEDIUM-HIGH/MEDIUM/LOW (one line rationale).\n"
            "PEP risk scoring guide:\n"
            "- MEDIUM-HIGH or higher if: billionaire UBO, government golden share/special share, "
            "any Trump/political family connection, Russian business ties, or multiple PEP-adjacent "
            "stakeholders\n"
            "- MEDIUM only if: minor institutional shareholders with no political connections\n"
            "If NO PEPs identified, state that in one line.\n"
            "No recommendation language. Facts only."
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

        # Check source quality for tag assignment.
        has_gov_or_sec = False
        has_any_text = False
        for sid in source_ids:
            src = sources_by_id.get(sid, {})
            url = src.get("url", "")
            if bool((src.get("content") or "").strip()):
                has_any_text = True
            tier = classify_source_tier(url)
            if tier == SourceTier.TIER_1 or "sec.gov" in url.lower():
                has_gov_or_sec = True

        # FACT with government/SEC source → CONFIRMED
        if ft == "fact" and has_gov_or_sec:
            return "CONFIRMED"
        # FACT with retrieved text from any source → REPORTED (verifiable)
        if ft == "fact" and has_any_text:
            return "REPORTED"
        # FACT with no text → REPORTED (still a factual claim, just unverifiable)
        if ft == "fact":
            return "REPORTED"
        # Analysis → REPORTED
        if ft == "analysis":
            return "REPORTED"
        # Advocacy → UNVERIFIED
        if ft == "advocacy":
            return "UNVERIFIED"
        # Interpretation → UNVERIFIED
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
        lines.append("\n" + "=" * 60)
        lines.append("MANDATORY PRIOR FINDINGS — MUST APPEAR IN REPORT")
        lines.append("=" * 60)
        lines.append("These findings are from prior manual investigations. They MUST be included "
                     "in the final report — omitting them is a FAILURE. If a finding mentions "
                     "checking the source list for a matching reference, scan the GLOBAL SOURCE "
                     "LIST below and cite the matching [n] if found.")
        for pf in prior:
            sections_note = ""
            if pf.get("include_in_sections"):
                sections_note = f" [INCLUDE IN: {', '.join(pf['include_in_sections'])}]"
            lines.append(f"\n  MANDATORY: [{pf['tag']}] ({pf['category']}){sections_note}")
            lines.append(f"  {pf['claim']}")
            lines.append(f"  Source: {pf['source_description']}")
        lines.append("=" * 60 + "\n")

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


def _fallback_company_overview(state: Dict[str, Any]) -> str:
    """Extract a minimal company overview from findings and narratives when the writer
    returns an empty section. Scans for company description keywords."""
    subject = state.get("subject", "Unknown")
    # Try to find overview-like content from raw narratives.
    for ao in state.get("raw_outputs", []):
        if ao.get("domain") == "overview_ownership":
            narrative = (ao.get("narrative_markdown") or "").strip()
            if narrative:
                # Take the first ~200 chars as a rough overview.
                first_para = narrative.split("\n\n")[0][:500]
                if len(first_para) > 30:
                    return first_para
    # Fallback: construct from findings mentioning the subject.
    findings = state.get("aggregated_findings", [])
    overview_claims = []
    for f in findings:
        claim = f.get("claim", "").lower()
        if any(kw in claim for kw in ("headquartered", "founded", "employees", "revenue",
                                       "stock", "nyse", "tase", "subsidiary", "operates")):
            overview_claims.append(f["claim"])
            if len(overview_claims) >= 3:
                break
    if overview_claims:
        return " ".join(overview_claims)
    return f"{subject} — company overview data not available from retrieved sources."


def _fallback_company_ownership(state: Dict[str, Any]) -> str:
    """Extract ownership info from findings when the writer returns blank."""
    findings = state.get("aggregated_findings", [])
    ownership_claims = []
    for f in findings:
        claim = f.get("claim", "").lower()
        if any(kw in claim for kw in ("shareholder", "ownership", "beneficial owner",
                                       "stake", "holds", "percent", "%", "shares")):
            sids = f.get("source_ids", [])
            cite = f" [{sids[0]}]" if sids else ""
            ownership_claims.append(f"- {f['claim']}{cite}")
            if len(ownership_claims) >= 6:
                break
    if ownership_claims:
        return "\n".join(ownership_claims)
    return "Ownership data not available from retrieved sources."


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
                    entry = {
                        "claim": finding["claim"],
                        "tag": finding.get("tag", "UNVERIFIED"),
                        "category": finding.get("category", "Uncategorized"),
                        "source_description": finding.get("source_description", "Prior manual due diligence"),
                    }
                    if finding.get("include_in_sections"):
                        entry["include_in_sections"] = finding["include_in_sections"]
                    results.append(entry)
        except Exception:
            continue
    return results


def _inject_missing_prior_findings(sections: List[Dict[str, Any]], subject: str) -> None:
    """Code-level guardrail: if must_include prior findings are missing from the report
    body, append them directly. This guarantees they appear regardless of LLM compliance."""
    import pathlib
    import yaml

    prior_dir = pathlib.Path(__file__).resolve().parents[2] / "config" / "prior_findings"
    if not prior_dir.is_dir():
        return

    subject_lower = subject.lower()
    sections_by_id = {s["id"]: s for s in sections}

    for f in prior_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text())
            if not isinstance(data, dict):
                continue
            patterns = data.get("subject_patterns", [])
            if not any(p.lower() in subject_lower for p in patterns):
                continue
            for finding in data.get("findings", []):
                if not isinstance(finding, dict) or not finding.get("must_include"):
                    continue
                claim = finding["claim"]
                tag = finding.get("tag", "UNVERIFIED")
                category = finding.get("category", "")
                # Check a signature phrase from the claim to see if LLM included it.
                # Use first 60 chars as a fingerprint.
                fingerprint = claim[:60].lower()

                target_sections = finding.get("include_in_sections", ["risk_issues"])
                for sid in target_sections:
                    sec = sections_by_id.get(sid)
                    if not sec:
                        continue
                    body = (sec.get("body_markdown") or "").lower()
                    if fingerprint in body:
                        continue  # Already present
                    # Append the finding to the section body.
                    # Mark as [unverified] for coverage accounting — these are manual DD
                    # findings without a numbered source in the source list.
                    bullet = f"\n- [{tag}] ({category}) {claim} [unverified — prior manual due diligence]\n"
                    sec["body_markdown"] = (sec.get("body_markdown") or "") + bullet
        except Exception:
            continue
