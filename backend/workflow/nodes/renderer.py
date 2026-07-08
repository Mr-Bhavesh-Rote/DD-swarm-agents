"""renderer node (§4.1 node 6).

Assembles the RAW report (per-agent narratives + full source list) and the FINAL report
(verified sections + numbered, hyperlinked citations). Both are emitted as JSON matching
the §5.5 / §5.6 contracts and persisted by the API/worker layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def renderer_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    run_id = state.get("run_id", "")
    subject = state["subject"]
    subject_type = state["subject_type"]
    now = datetime.now(timezone.utc).isoformat()

    sources = state.get("sources", [])
    # Wire sources omit the bulky `content` field (kept in DB / used only by verifier).
    # Also filter out Wikipedia sources — they must never appear in the final report.
    wire_sources = [
        {k: v for k, v in s.items() if k != "content"}
        for s in sources
        if "wikipedia.org" not in (s.get("url") or "").lower()
    ]

    raw_report = {
        "run_id": run_id,
        "subject": subject,
        "subject_type": subject_type,
        "generated_at": now,
        "agent_outputs": state.get("raw_outputs", []),
        "sources": wire_sources,
    }

    sections = state.get("draft_sections", [])
    import re

    cite_re = re.compile(r"\[(\d+)\]")
    valid_source_ids = {s.get("id") for s in wire_sources}

    # Strip orphaned citations: remove [n] markers from body where n doesn't exist
    # in the source list (LLM hallucinated the citation ID).
    # Also strip [F:n] internal finding references that leaked into the output.
    finding_ref_re = re.compile(r"\[F:\d+\]")

    def _strip_orphaned(body: str) -> str:
        # First remove [F:xx] internal finding references
        body = finding_ref_re.sub("", body or "")
        def _repl(m: re.Match) -> str:
            cid = int(m.group(1))
            return m.group(0) if cid in valid_source_ids else ""
        return cite_re.sub(_repl, body)

    # Strip negative-search-result lines from Unverified sections and remove
    # lines about unnamed/unidentified companies (hallucination risk).
    _negative_re = re.compile(
        r"(no (entries|records|results|listings?) found)"
        r"|(not (found|identified|listed) (on|in))"
        r"|(does not appear)"
        r"|(no evidence)"
        r"|(returned no)"
        r"|(unnamed|unidentified) (company|firm|entity)",
        re.IGNORECASE,
    )

    def _clean_unverified_lines(body: str) -> str:
        """Remove negative-result and unnamed-entity lines from body text."""
        cleaned = []
        for line in body.split("\n"):
            stripped = line.strip()
            # Only filter bullet points (- or *), not headers or other structure
            if stripped.startswith(("-", "*")) and _negative_re.search(stripped):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    all_cited_ids: set[int] = set()
    for sec in sections:
        sec["body_markdown"] = _strip_orphaned(sec.get("body_markdown", "") or "")
        # Clean negative results from unverified subsections
        sec["body_markdown"] = _clean_unverified_lines(sec.get("body_markdown", "") or "")
        ids = sorted({int(x) for x in cite_re.findall(sec.get("body_markdown", "") or "")})
        sec["citations"] = ids
        all_cited_ids.update(ids)

    # Final report only includes sources actually cited in the body — uncited research
    # sources bloat the references list and confuse reviewers.
    # Also flag sources where scraper failed to retrieve content.
    sources_full = {s["id"]: s for s in sources}  # includes content field
    final_sources = []
    for s in wire_sources:
        if s.get("id") not in all_cited_ids:
            continue
        full = sources_full.get(s["id"], {})
        has_content = bool((full.get("content") or "").strip())
        enriched = dict(s)
        if not has_content:
            enriched["unreachable"] = True
        final_sources.append(enriched)

    source_manifest = _build_source_manifest(state)
    quality_assessment = state.get("quality_assessment", {})
    final_report = {
        "run_id": run_id,
        "subject": subject,
        "subject_type": subject_type,
        "generated_at": now,
        "model_summary": state.get("model_summary", {}),
        "verification": state.get("verification", {"citation_coverage": 0.0, "faithfulness_score": 0.0, "flags": []}),
        "quality_assessment": quality_assessment,
        "source_manifest": source_manifest,
        "sections": sections,
        "sources": final_sources,
    }

    return {
        "raw_report": raw_report,
        "final_report": final_report,
        "events": [{"node": "renderer", "status": "completed",
                    "n_sections": len(sections), "n_sources": len(wire_sources)}],
    }


def _build_source_manifest(state: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate which compliance-source tools were called across all research agents.
    Keyed by agent domain so template and AI-tailored runs produce comparable manifests."""
    from workflow.nodes.research import REQUIRED_TOOLS_BY_DOMAIN

    # Map domain -> set of tools called by agents of that domain.
    called_by_domain: Dict[str, set] = {}
    for ao in state.get("raw_outputs", []) or []:
        domain = ao.get("domain") or "overview_ownership"
        called_by_domain.setdefault(domain, set()).update(
            c.get("tool") for c in (ao.get("tool_calls") or [])
        )

    manifest: Dict[str, Any] = {}
    for domain, required in REQUIRED_TOOLS_BY_DOMAIN.items():
        called = called_by_domain.get(domain, set())
        for tool in required:
            manifest[tool] = {
                "required_by": domain,
                "attempted": tool in called,
            }
    return manifest


def render_markdown(report: Dict[str, Any], kind: str) -> str:
    """Render a report dict to markdown (used for reports.report_markdown + exporters)."""
    if kind == "raw":
        return _raw_markdown(report)
    return _final_markdown(report)


def _final_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = [f"# Due-Diligence Report — {report['subject']}", ""]
    src_by_id = {s["id"]: s for s in report.get("sources", [])}
    for sec in report.get("sections", []):
        lines.append(f"## {sec['title']}")
        lines.append(_linkify(sec.get("body_markdown", ""), src_by_id))
        for t in sec.get("tables", []):
            lines.append("")
            lines.append(_md_table(t))
        lines.append("")

    lines.append("## Sources Queried")
    manifest = report.get("source_manifest", {})
    if manifest:
        for tool, info in sorted(manifest.items()):
            status = "queried" if info.get("attempted") else "NOT queried"
            lines.append(f"- **{tool}** ({info.get('required_by', '')}): {status}")
    else:
        lines.append("- No source manifest recorded.")
    lines.append("")

    lines.append("## References")
    for s in report.get("sources", []):
        tag = " ⚠️ LINK UNAVAILABLE" if s.get("unreachable") else ""
        lines.append(f"[{s['id']}] [{s.get('title') or s['url']}]({s['url']}){tag}")
    return "\n".join(lines)


def _raw_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = [f"# RAW Research Output — {report['subject']}", ""]
    for ao in report.get("agent_outputs", []):
        lines.append(f"## {ao.get('role') or ao['agent']}  ({ao.get('model','')})")
        lines.append(ao.get("narrative_markdown", ""))
        if ao.get("findings"):
            lines.append("\n**Findings:**")
            for f in ao["findings"]:
                ids = f.get("source_ids") or f.get("source_urls") or []
                lines.append(f"- {f.get('claim','')} {ids}")
        lines.append("")
    lines.append("## Sources")
    for s in report.get("sources", []):
        lines.append(f"[{s['id']}] [{s.get('title') or s['url']}]({s['url']})")
    return "\n".join(lines)


def _linkify(body: str, src_by_id: Dict[int, Dict[str, Any]]) -> str:
    import re

    def repl(m: "re.Match") -> str:
        cid = int(m.group(1))
        src = src_by_id.get(cid)
        return f"[[{cid}]]({src['url']})" if src else m.group(0)

    return re.sub(r"\[(\d+)\]", repl, body or "")


def _md_table(t: Dict[str, Any]) -> str:
    cols = t.get("columns", [])
    if not cols:
        return ""
    rows = t.get("rows", [])
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    title = t.get("title")
    return (f"**{title}**\n\n" if title else "") + "\n".join(out)
