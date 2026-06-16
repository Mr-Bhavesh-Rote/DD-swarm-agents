"""PDF export via WeasyPrint (§9).

Renders the report HTML (same content as the viewer) to PDF, preserving section headers,
tables and clickable [n] citation links + a References appendix.
"""
from __future__ import annotations

from typing import Any, Dict

from exporters.html import report_to_html


def render_pdf(report: Dict[str, Any], kind: str) -> bytes:
    from weasyprint import HTML

    html_str = report_to_html(report, kind)
    return HTML(string=html_str).write_pdf()
