"""Quality gate node — runs between verifier and renderer.

Executes all 4 quality phases on the verified report:
  1. Source quality assessment (tier classification + retrievability)
  2. Finding classification (fact / analysis / interpretation / advocacy)
  3. Circular dependency detection
  4. Quality gates (4 automated pass/fail checks)

Writes a `quality_assessment` dict to the graph state and emits events for the UI.
The renderer and persistence layer use this to annotate the final report.
"""
from __future__ import annotations

from typing import Any, Dict, List

from workflow.quality.circular_deps import (
    circular_dep_summary,
    detect_all_circular_deps,
)
from workflow.quality.fact_classifier import classify_all_findings, segment_findings
from workflow.quality.gates import run_all_gates
from workflow.quality.source_tiers import (
    assess_all_sources,
    compute_finding_confidence,
)


def quality_gate_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Run the full quality assessment pipeline on the current report state."""
    subject = state["subject"]
    findings = list(state.get("aggregated_findings", []))
    sources = state.get("sources", [])
    verification = state.get("verification", {})

    sources_by_id = {s["id"]: s for s in sources}

    # Phase 1: Source quality assessment.
    source_assessment = assess_all_sources(sources, subject)

    # Compute per-finding confidence based on source tiers.
    for f in findings:
        src_records = [sources_by_id[sid] for sid in f.get("source_ids", []) if sid in sources_by_id]
        f["confidence_assessment"] = compute_finding_confidence(
            src_records, subject, has_circular_dep=False,  # will be refined in phase 3
        )

    # Phase 2: Fact vs. interpretation classification.
    findings = classify_all_findings(findings, sources_by_id, subject)
    segmentation = segment_findings(findings)

    # Phase 3: Circular dependency detection.
    findings = detect_all_circular_deps(findings, sources_by_id, subject)
    cd_summary = circular_dep_summary(findings)

    # Re-compute confidence now that circular deps are known.
    for f in findings:
        has_cd = f.get("circular_dep", {}).get("has_circular_dep", False)
        if has_cd:
            src_records = [sources_by_id[sid] for sid in f.get("source_ids", []) if sid in sources_by_id]
            f["confidence_assessment"] = compute_finding_confidence(
                src_records, subject, has_circular_dep=True,
            )

    # Phase 4: Quality gates.
    gate_results = run_all_gates(findings, sources, verification)

    # Build the full quality assessment.
    quality_assessment = {
        "source_assessment": source_assessment,
        "finding_segmentation": {
            "core": len(segmentation["core"]),
            "analysis": len(segmentation["analysis"]),
            "unverified": len(segmentation["unverified"]),
            "advocacy": len(segmentation["advocacy"]),
        },
        "circular_dependency_summary": cd_summary,
        "quality_gates": gate_results,
        "enriched_findings": _strip_for_storage(findings),
    }

    return {
        "quality_assessment": quality_assessment,
        "events": [{
            "node": "quality_gate",
            "status": "completed",
            "quality_score": gate_results["quality_score"],
            "report_status": gate_results["status"],
            "gates_passed": gate_results["gates_passed"],
            "gates_total": gate_results["gates_total"],
        }],
    }


def _strip_for_storage(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip enriched findings to only the quality-relevant fields for storage."""
    result = []
    for f in findings:
        result.append({
            "claim": f.get("claim", "")[:300],
            "source_ids": f.get("source_ids", []),
            "confidence": f.get("confidence", "medium"),
            "category": f.get("category"),
            "finding_type": f.get("finding_type"),
            "confidence_assessment": {
                "score": f.get("confidence_assessment", {}).get("score", 0),
                "level": f.get("confidence_assessment", {}).get("level", "low"),
                "penalties": f.get("confidence_assessment", {}).get("penalties", []),
            },
            "circular_dep": {
                "has_circular_dep": f.get("circular_dep", {}).get("has_circular_dep", False),
                "severity": f.get("circular_dep", {}).get("severity", "none"),
                "recommended_action": f.get("circular_dep", {}).get("recommended_action", "include"),
            },
        })
    return result
