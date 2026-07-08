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
            if sec["id"] == "pep_status" and not (sec.get("body_markdown") or "").strip():
                sections[i]["body_markdown"] = _fallback_pep_status(state)

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

    # (A2) Fix missing citation numbers: the LLM sometimes writes "[REPORTED — The Guardian]"
    # but forgets to add a [n] citation. Scan for these and inject the matching source ID.
    sources_list = state.get("sources", [])
    if sources_list:
        for i, sec in enumerate(sections):
            body = sec.get("body_markdown", "") or ""
            if body.strip():
                sections[i]["body_markdown"] = _inject_missing_citations(body, sources_list)

    # (B0) Code-level hallucination filter: strip draft bullets that cannot be traced
    # to any finding in the aggregated findings list. This is the ONLY reliable defence
    # against recurring hallucinations (fatal accidents, wrong SEC cases, etc.) because
    # prompt-level rules are consistently ignored by the LLM.
    agg_findings = state.get("aggregated_findings", [])
    if agg_findings:
        for i, sec in enumerate(sections):
            body = sec.get("body_markdown", "") or ""
            if not body.strip():
                continue
            cleaned = _strip_hallucinated_bullets(body, agg_findings, state.get("raw_outputs", []))
            if cleaned != body:
                sections[i]["body_markdown"] = cleaned

    # (B) Pre-verify citation coverage: redraft any section below 60% coverage ONCE.
    # This catches the "52% uncited" problem before the verifier even runs, saving a
    # full verifier→revision round-trip for what is essentially a formatting issue.
    import re
    _cite_check = re.compile(r"\[(\d+)\]")
    _skip_re = re.compile(
        r"^(\s*#{1,6}\s)|^(\s*---)|^(\s*\*\*[^*]+\*\*\s*$)"
    )
    _unverified_re = re.compile(r"\[(unverified|estimate)[^\]]*\]", re.IGNORECASE)

    def _count_coverage(body: str) -> tuple[int, int]:
        """Count (total_statements, cited_statements) using line-aware splitting."""
        total = cited = 0
        for line in body.split("\n"):
            s = line.strip()
            if len(s) < 12 or _skip_re.match(s):
                continue
            total += 1
            if _cite_check.search(s) or _unverified_re.search(s):
                cited += 1
        return total, cited

    for i, sec in enumerate(sections):
        body = sec.get("body_markdown", "") or ""
        total, cited = _count_coverage(body)
        if not total:
            continue
        coverage = cited / total
        if coverage < 0.8:
            sid, title = sec["id"], sec["title"]
            uncited = [l.strip()[:100] for l in body.split("\n")
                       if len(l.strip()) >= 12 and not _cite_check.search(l)
                       and not _skip_re.match(l.strip()) and not _unverified_re.search(l)][:5]
            gap_feedback = (
                f"CITATION GAP: This section has only {coverage:.0%} citation coverage "
                f"({cited}/{total} statements cited). "
                f"Examples of UNCITED statements that MUST be fixed:\n"
                + "\n".join(f"- \"{ex}...\"" for ex in uncited)
                + "\n\nRewrite the section so that EVERY factual line ends with [n] citations. "
                "Drop any claim you cannot cite."
            )
            redraft, redraft_cost = _draft_one_section(
                writer_model, subject, subject_type, task, sid, title, shared,
                section_flags=[{"section_id": sid, "claim": "uncited sentences",
                                "citation_ids": [], "reason": gap_feedback}],
                callbacks=callbacks,
            )
            redraft_body = redraft.get("body_markdown", "") or ""
            r_total, r_cited = _count_coverage(redraft_body)
            if r_total and (r_cited / r_total) > coverage:
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
        "Only cite a source if it DIRECTLY supports the specific claim — "
        "do NOT cite loosely related sources. NEVER reuse the same citation for unrelated claims. "
        "Check each source's title/URL before citing to confirm it matches your claim's topic."
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
            "or official government publications. This section MUST include:\n"
            "- Sanctions screening CLEAN results (OFAC SDN, OFAC Non-SDN, BIS Entity List, "
            "UN, EU checked — subject not found). Cite the database source [n] from the source list.\n"
            "- Any government enforcement actions, court rulings, or regulatory findings.\n"
            "This section must NOT be empty — at minimum, sanctions screening results go here.\n\n"
            "### REPORTED ALLEGATIONS [REPORTED]\n"
            "Claims from credible journalists, NGOs (HRW, Amnesty), or advocacy organizations. "
            "Also: SEC filing self-disclosures of adverse risk factors. "
            "Format: '[REPORTED — source name]' e.g. '[REPORTED — Human Rights Watch]'\n\n"
            "### UNVERIFIED ITEMS [UNVERIFIED]\n"
            "ONLY include items here if they are MATERIAL adverse findings where the source "
            "could not be verified. Do NOT include:\n"
            "- Negative search results ('no entries found', 'not on list') — those are CONFIRMED negatives\n"
            "- Items where you simply couldn't retrieve the source — OMIT these entirely\n"
            "- 'No evidence found' statements — omit, do not publish absence of evidence\n"
            "Only include substantive claims from a named source that could not be independently verified.\n\n"
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
            "→ [CONFIRMED] — cite the actual database URLs (sanctionssearch.ofac.treas.gov, "
            "bis.gov, un.org, etc.) from the source list, NOT news articles about sanctions. "
            "List ALL databases checked in one consolidated bullet.\n\n"
            "ANTI-HALLUCINATION CHECK (CRITICAL):\n"
            "- Before writing ANY bullet about a fatality, death, accident, explosion, or spill, "
            "search the findings list for that EXACT event. If it is not there, DO NOT WRITE IT.\n"
            "- Before writing ANY dollar figure, find the EXACT figure in the findings list and "
            "copy it verbatim. Do not round or approximate.\n\n"
            "CITATION ACCURACY RULES (CRITICAL — violations tank faithfulness scores):\n"
            "- Each citation [n] MUST directly support the SPECIFIC claim it is attached to. "
            "Read the source title and URL before citing — if the source is about a DIFFERENT "
            "topic, event, or entity than your claim, DO NOT cite it.\n"
            "- NEVER reuse a citation [n] across unrelated claims. An article about environmental "
            "fines does NOT support a claim about native title, greenwashing, or SEC proceedings.\n"
            "- For sanctions screening results, ONLY cite the actual sanctions database source "
            "(sanctionssearch.ofac.treas.gov, etc.) — do NOT cite news articles about sanctions.\n"
            "- For regulatory enforcement (Good Jobs First, ASIC, SEC), cite the ACTUAL regulatory "
            "source, not an unrelated news article. Do NOT conflate Australian ASIC proceedings "
            "with US SEC enforcement or Good Jobs First records.\n"
            "- If no matching source exists in the source list for a claim, mark the claim as "
            "[unverified] rather than attaching a wrong citation.\n"
            "- BEFORE writing each bullet, CHECK that your citation [n] matches: scan the source "
            "list for [n], read its title/URL, confirm it is about the SAME event as your claim.\n\n"
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
    lines: List[str] = [
        "⚠️ CITATION FORMAT: Use ONLY [n] format (e.g. [1], [2], [34]) to cite sources. "
        "The [n] numbers correspond to the GLOBAL SOURCE LIST at the bottom. "
        "Each finding below ends with 'CITE AS: [n]' — copy those exact numbers into your report. "
        "Do NOT use [F:n] or any other format. Do NOT invent citation numbers.\n"
        "\n⚠️ FAITHFULNESS: ONLY write claims that appear in the findings below. "
        "Do NOT add your own knowledge.\n"
    ]

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
            # Format source IDs as cite-ready [n] markers so the LLM copies them directly.
            src_ids = f.get('source_ids') or []
            cite_markers = " ".join(f"[{sid}]" for sid in src_ids) if src_ids else "[no source]"
            lines.append(
                f"- [{tag}] ({cat}, confidence={conf_label}{cd_tag}) "
                f"{f['claim']} — CITE AS: {cite_markers}"
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


def _fallback_pep_status(state: Dict[str, Any]) -> str:
    """Build a structured PEP section from findings when the writer returns blank.

    Scans ALL findings for person names with government/political/board connections
    and formats them with PEP level ratings and sanctions status.
    """
    findings = state.get("aggregated_findings", [])

    # Broad keyword scan — catch PEP, board, ownership, political connections.
    _PEP_KW = ("pep", "politically exposed", "government", "minister", "politician",
                "state-owned", "sovereign", "chairman", "board member", "director",
                "non-executive", "executive", "founder", "ceo", "managing director",
                "billionaire", "net worth", "golden share", "special share",
                "shareholder", "beneficial owner", "stake", "ownership")
    _PEP_CAT_KW = ("pep", "ownership", "political", "state", "governance")

    pep_findings = []
    for f in findings:
        claim = f.get("claim", "").lower()
        cat = (f.get("category") or "").lower()
        if any(kw in claim for kw in _PEP_KW) or any(kw in cat for kw in _PEP_CAT_KW):
            pep_findings.append(f)

    if not pep_findings:
        return "No PEP-related findings identified from retrieved sources.\n\nOVERALL PEP RISK: LOW — no politically exposed persons identified."

    lines: List[str] = []

    # Separate ownership/shareholder findings from person-level PEP findings.
    person_findings = []
    ownership_findings = []
    for f in pep_findings:
        claim_lower = f.get("claim", "").lower()
        if any(kw in claim_lower for kw in ("shareholder", "ownership", "stake", "beneficial owner", "%")):
            ownership_findings.append(f)
        else:
            person_findings.append(f)

    # Emit person-level PEP entries with structured format.
    if person_findings:
        for f in person_findings[:10]:
            sids = f.get("source_ids", [])
            cite = " " + " ".join(f"[{sid}]" for sid in sids) if sids else ""
            lines.append(f"- {f['claim']}{cite}")

    # Emit ownership context.
    if ownership_findings:
        if person_findings:
            lines.append("")
        lines.append("**Key Shareholders:**")
        for f in ownership_findings[:6]:
            sids = f.get("source_ids", [])
            cite = " " + " ".join(f"[{sid}]" for sid in sids) if sids else ""
            lines.append(f"- {f['claim']}{cite}")

    # Determine PEP risk level heuristically from findings content.
    all_text = " ".join(f.get("claim", "").lower() for f in pep_findings)
    if any(kw in all_text for kw in ("billionaire", "golden share", "special share",
                                      "president", "prime minister", "head of state",
                                      "sanctioned", "trump")):
        risk_level = "HIGH"
        rationale = "billionaire UBO, government connections, or head-of-state ties identified"
    elif any(kw in all_text for kw in ("minister", "politician", "government official",
                                        "state-owned", "sovereign")):
        risk_level = "MEDIUM-HIGH"
        rationale = "government or political connections identified among key persons"
    elif any(kw in all_text for kw in ("board member", "director", "non-executive",
                                        "chairman")):
        risk_level = "MEDIUM"
        rationale = "board-level connections identified requiring PEP assessment"
    else:
        risk_level = "MEDIUM"
        rationale = "ownership figures identified with potential PEP exposure"

    lines.append(f"\nOVERALL PEP RISK: {risk_level} — {rationale}.")

    return "\n".join(lines)


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


def _inject_missing_citations(body: str, sources: List[Dict[str, Any]]) -> str:
    """Fix lines that have [REPORTED — source] or [CONFIRMED] tags but no [n] citation.

    Scans each bullet for lines missing a numeric citation and tries to match a source
    from the source list by name/publication in the tag or line text.
    """
    import re

    _has_cite = re.compile(r"\[\d+\]")
    _source_tag = re.compile(r"\[(?:REPORTED|CONFIRMED|UNVERIFIED)\s*(?:—\s*([^\]]+))?\]", re.IGNORECASE)

    # Build lookup: lowercase publication/domain → source id.
    pub_to_ids: Dict[str, List[int]] = {}
    for s in sources:
        title = (s.get("title") or "").lower()
        url = (s.get("url") or "").lower()
        # Extract domain name.
        domain_parts = url.replace("https://", "").replace("http://", "").split("/")[0].split(".")
        for part in domain_parts:
            if len(part) > 3 and part not in ("www", "com", "org", "gov", "net", "edu"):
                pub_to_ids.setdefault(part, []).append(s["id"])
        # Extract key words from title.
        for word in re.findall(r"[a-z]{4,}", title):
            if word not in ("article", "news", "report", "about", "with", "from", "that", "this"):
                pub_to_ids.setdefault(word, []).append(s["id"])

    lines = body.split("\n")
    result: List[str] = []

    for line in lines:
        stripped = line.strip()
        # Only process bullet lines that are missing citations.
        if stripped.startswith(("-", "*")) and len(stripped) > 20 and not _has_cite.search(stripped):
            # Try to find a source match from the tag or line text.
            tag_match = _source_tag.search(stripped)
            search_text = stripped.lower()
            if tag_match and tag_match.group(1):
                search_text = tag_match.group(1).lower() + " " + search_text

            # Score each source by keyword overlap.
            best_id = None
            best_score = 0
            seen_ids: set = set()
            for word in re.findall(r"[a-z]{4,}", search_text):
                for sid in pub_to_ids.get(word, []):
                    if sid not in seen_ids:
                        seen_ids.add(sid)
            # For each candidate source, count how many of its title words appear in the line.
            for s in sources:
                if s["id"] not in seen_ids:
                    continue
                s_title = (s.get("title") or "").lower()
                s_words = set(re.findall(r"[a-z]{4,}", s_title))
                line_words = set(re.findall(r"[a-z]{4,}", search_text))
                overlap = len(s_words & line_words)
                if overlap > best_score:
                    best_score = overlap
                    best_id = s["id"]

            if best_id and best_score >= 2:
                # Append citation at end of line.
                line = line.rstrip() + f" [{best_id}]"

        result.append(line)

    return "\n".join(result)


def _strip_hallucinated_bullets(
    body: str,
    findings: List[Dict[str, Any]],
    raw_outputs: List[Dict[str, Any]],
) -> str:
    """Remove bullet points from the draft that cannot be traced to any finding or narrative.

    The LLM consistently hallucinates events (fatal accidents, wrong court cases) despite
    prompt instructions. This code-level filter is the only reliable defence.
    """
    import re

    # Build a searchable text corpus from all findings + narratives.
    corpus_parts: List[str] = []
    for f in findings:
        corpus_parts.append((f.get("claim") or "").lower())
    for ao in raw_outputs:
        corpus_parts.append((ao.get("narrative_markdown") or "").lower())
    corpus = "\n".join(corpus_parts)

    # High-risk hallucination patterns — if a bullet matches these AND can't be found
    # in the corpus, it's almost certainly fabricated.
    _HALLUCINATION_PATTERNS = re.compile(
        r"(fatal|fatality|death|killed|died)"
        r"|(work(place|er)?\s+(accident|incident|safety\s+death))"
        r"|(explosion|collapsed|crushed)",
        re.IGNORECASE,
    )

    lines = body.split("\n")
    cleaned: List[str] = []

    for line in lines:
        stripped = line.strip()

        # Always keep non-bullet lines (headers, blank lines, structural elements).
        if not stripped.startswith(("-", "*")) or len(stripped) < 20:
            cleaned.append(line)
            continue

        # Always keep sanctions screening results and [unverified] markers.
        lower = stripped.lower()
        if any(kw in lower for kw in ("ofac", "sdn", "bis entity", "un sanctions",
                                       "eu sanctions", "not found on", "not listed",
                                       "unverified", "prior manual")):
            cleaned.append(line)
            continue

        # Check if this bullet contains high-risk hallucination patterns.
        if _HALLUCINATION_PATTERNS.search(stripped):
            # For high-risk claims, require strong corpus match.
            clean_text = re.sub(r"\[\d+\]", "", stripped)
            clean_text = re.sub(r"\[(CONFIRMED|REPORTED|UNVERIFIED)[^\]]*\]", "", clean_text, flags=re.IGNORECASE)
            clean_text = re.sub(r"^[-*]\s*", "", clean_text).strip().lower()

            # Extract distinctive words (4+ chars, skip common/stop words).
            _STOP = {"that", "this", "with", "from", "were", "been", "have", "will",
                     "also", "which", "their", "than", "into", "over", "such", "more",
                     "against", "between", "through", "about", "group", "company",
                     "mining", "metals", "fortescue"}
            words = [w for w in re.findall(r"[a-z]{4,}", clean_text) if w not in _STOP]

            if not words:
                cleaned.append(line)
                continue

            # Require at least 40% of distinctive words to appear in corpus.
            matched = sum(1 for w in words if w in corpus)
            ratio = matched / len(words) if words else 0

            if ratio < 0.4:
                continue  # Hallucination detected — strip this bullet.

        cleaned.append(line)

    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result
