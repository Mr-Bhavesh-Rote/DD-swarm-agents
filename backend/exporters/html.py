"""Shared HTML rendering for exports (§9).

Renders FINAL/RAW report JSON to standalone HTML with clickable [n] citation links and a
References appendix. Used by the WeasyPrint PDF exporter and reusable by a server-side
print path.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List

import markdown as md

_CITE_RE = re.compile(r"\[(\d+)\]")

_CSS = """
body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 11pt; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 22pt; border-bottom: 2px solid #333; padding-bottom: 6px; }
h2 { font-size: 15pt; color: #11366b; margin-top: 24px; border-bottom: 1px solid #ccc; }
h3 { font-size: 12pt; color: #333; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt; }
th, td { border: 1px solid #999; padding: 4px 8px; text-align: left; }
th { background: #eef2f8; }
a.cite { color: #11366b; text-decoration: none; font-weight: bold; vertical-align: super; font-size: 8pt; }
.meta { color: #666; font-style: italic; margin-bottom: 16px; }
.disclaimer { background: #fff7e6; border: 1px solid #ffd591; padding: 8px 12px; font-size: 9pt; margin: 16px 0; }
.references li { margin-bottom: 4px; word-break: break-all; }
"""

DISCLAIMER = (
    "This report is decision-support, not a background-check product. It is generated from "
    "public sources and must be human-reviewed before informing any decision. Estimates and "
    "unverified items are labelled; verify independently before relying on them."
)


def report_to_html(report: Dict[str, Any], kind: str) -> str:
    src_by_id = {s["id"]: s for s in report.get("sources", [])}
    subject = html.escape(report.get("subject", ""))
    title = "RAW Research Output" if kind == "raw" else "Due-Diligence Report"

    parts: List[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{title} — {subject}</h1>",
        f"<div class='meta'>Generated {html.escape(report.get('generated_at', ''))}</div>",
        f"<div class='disclaimer'>{html.escape(DISCLAIMER)}</div>",
    ]

    if kind == "raw":
        parts.append(_raw_body(report))
    else:
        parts.append(_final_body(report, src_by_id))

    parts.append(_references(report.get("sources", [])))
    parts.append("</body></html>")
    return "".join(parts)


def _final_body(report: Dict[str, Any], src_by_id: Dict[int, Dict[str, Any]]) -> str:
    v = report.get("verification", {})
    out = [
        f"<div class='meta'>Citation coverage: {v.get('citation_coverage', 0):.0%} · "
        f"Faithfulness: {v.get('faithfulness_score', 0):.0%}</div>"
    ]
    for sec in report.get("sections", []):
        out.append(f"<h2>{html.escape(sec.get('title', ''))}</h2>")
        body_html = md.markdown(sec.get("body_markdown", ""), extensions=["tables"])
        out.append(_linkify_html(body_html, src_by_id))
        for t in sec.get("tables", []):
            out.append(_table_html(t))
    return "".join(out)


def _raw_body(report: Dict[str, Any]) -> str:
    out = []
    for ao in report.get("agent_outputs", []):
        out.append(f"<h2>{html.escape(ao.get('role') or ao.get('agent', ''))} "
                   f"<small>({html.escape(ao.get('model', ''))})</small></h2>")
        out.append(md.markdown(ao.get("narrative_markdown", ""), extensions=["tables"]))
        findings = ao.get("findings", [])
        if findings:
            out.append("<h3>Findings</h3><ul>")
            for f in findings:
                ids = f.get("source_ids") or f.get("source_urls") or []
                out.append(f"<li>{html.escape(f.get('claim', ''))} <em>{html.escape(str(ids))}</em></li>")
            out.append("</ul>")
    return "".join(out)


def _linkify_html(body_html: str, src_by_id: Dict[int, Dict[str, Any]]) -> str:
    def repl(m: "re.Match") -> str:
        cid = int(m.group(1))
        src = src_by_id.get(cid)
        if not src:
            return m.group(0)
        return f"<a class='cite' href='{html.escape(src['url'])}'>[{cid}]</a>"

    return _CITE_RE.sub(repl, body_html)


def _table_html(t: Dict[str, Any]) -> str:
    cols = t.get("columns", [])
    if not cols:
        return ""
    out = [f"<p><strong>{html.escape(t.get('title', ''))}</strong></p>" if t.get("title") else "", "<table><tr>"]
    out += [f"<th>{html.escape(str(c))}</th>" for c in cols]
    out.append("</tr>")
    for row in t.get("rows", []):
        out.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _references(sources: List[Dict[str, Any]]) -> str:
    out = ["<h2>References</h2><ol class='references'>"]
    for s in sources:
        label = html.escape(s.get("title") or s["url"])
        out.append(f"<li id='ref-{s['id']}'>[{s['id']}] <a href='{html.escape(s['url'])}'>{label}</a></li>")
    out.append("</ol>")
    return "".join(out)
