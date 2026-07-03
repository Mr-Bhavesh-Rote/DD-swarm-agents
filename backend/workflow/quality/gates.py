"""Phase 4: Quality gates framework.

Implements 4 automated quality gates that must pass before a report is considered
ready for publication:

  Gate 1: Verification Completeness (>=70% of findings verified)
  Gate 2: Citation Accuracy (>=85% of claims match sources, via verifier)
  Gate 3: No Phantom Citations (unverified sources explicitly flagged)
  Gate 4: No High-Severity Circular Dependencies

Reports receive a status: PASS / PASS_WITH_CAVEATS / FAIL and an overall quality
score (0-100).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class ReportStatus(str, Enum):
    PASS = "pass"                       # All 4 gates passed
    PASS_WITH_CAVEATS = "pass_with_caveats"  # 3/4 gates passed
    FAIL = "fail"                       # <3 gates passed


# ---------------------------------------------------------------------------
# Individual gate evaluators
# ---------------------------------------------------------------------------

def gate_verification_completeness(
    findings: List[Dict[str, Any]],
    verification: Dict[str, Any],
    threshold: float = 0.70,
) -> Dict[str, Any]:
    """Gate 1: At least `threshold` of findings should be verified (have retrievable sources).

    Uses the faithfulness_score from the verifier as a proxy for verification completeness.
    """
    faithfulness = verification.get("faithfulness_score", 0.0)
    n_flags = len(verification.get("flags", []))
    total_findings = len(findings)

    # Also count findings that have at least one retrieved source.
    verified_count = 0
    for f in findings:
        confidence = f.get("confidence_assessment", {})
        if confidence:
            score = confidence.get("score", 0)
            if score >= 30:  # at least medium confidence
                verified_count += 1
        else:
            # Fallback: if no confidence assessment, count findings with sources.
            if f.get("source_ids"):
                verified_count += 1

    verification_rate = verified_count / max(total_findings, 1)
    passed = verification_rate >= threshold

    return {
        "gate": "verification_completeness",
        "status": GateStatus.PASS.value if passed else GateStatus.FAIL.value,
        "threshold": threshold,
        "actual": round(verification_rate, 4),
        "verified_findings": verified_count,
        "total_findings": total_findings,
        "faithfulness_score": faithfulness,
        "n_flags": n_flags,
        "message": (
            f"{verified_count}/{total_findings} findings verified ({verification_rate:.0%})"
            if passed else
            f"Only {verified_count}/{total_findings} findings verified ({verification_rate:.0%}); "
            f"threshold is {threshold:.0%}"
        ),
    }


def gate_citation_accuracy(
    verification: Dict[str, Any],
    threshold: float = 0.85,
) -> Dict[str, Any]:
    """Gate 2: Citation accuracy must be >= threshold.

    Uses the faithfulness_score from the LLM-as-judge verifier.
    """
    faithfulness = verification.get("faithfulness_score", 0.0)
    coverage = verification.get("citation_coverage", 0.0)
    n_flags = len(verification.get("flags", []))
    passed = faithfulness >= threshold

    return {
        "gate": "citation_accuracy",
        "status": GateStatus.PASS.value if passed else GateStatus.FAIL.value,
        "threshold": threshold,
        "actual": faithfulness,
        "citation_coverage": coverage,
        "n_unsupported_claims": n_flags,
        "message": (
            f"Citation accuracy {faithfulness:.0%} meets threshold {threshold:.0%}"
            if passed else
            f"Citation accuracy {faithfulness:.0%} below threshold {threshold:.0%}; "
            f"{n_flags} unsupported claim(s)"
        ),
    }


def gate_no_phantom_citations(
    sources: List[Dict[str, Any]],
    max_phantom_rate: float = 0.30,
) -> Dict[str, Any]:
    """Gate 3: Phantom citations (sources with no retrievable text) must be below threshold.

    A phantom citation is a source that was cited in the report but has no retrieved
    content for the verifier to check against.
    """
    total = len(sources)
    if total == 0:
        return {
            "gate": "no_phantom_citations",
            "status": GateStatus.FAIL.value,
            "threshold": max_phantom_rate,
            "actual": 1.0,
            "phantom_count": 0,
            "total_sources": 0,
            "phantom_sources": [],
            "message": "No sources in report",
        }

    phantom_sources: List[Dict[str, Any]] = []
    for s in sources:
        content = (s.get("content") or "").strip()
        snippet = (s.get("snippet") or "").strip()
        if not content and not snippet:
            phantom_sources.append({
                "id": s.get("id"),
                "url": s.get("url", ""),
                "title": s.get("title", ""),
            })

    phantom_rate = len(phantom_sources) / total
    passed = phantom_rate <= max_phantom_rate

    return {
        "gate": "no_phantom_citations",
        "status": GateStatus.PASS.value if passed else GateStatus.FAIL.value,
        "threshold": max_phantom_rate,
        "actual": round(phantom_rate, 4),
        "phantom_count": len(phantom_sources),
        "total_sources": total,
        "phantom_sources": phantom_sources[:10],  # cap for readability
        "message": (
            f"{len(phantom_sources)}/{total} phantom citations ({phantom_rate:.0%})"
            if passed else
            f"{len(phantom_sources)}/{total} sources have no retrievable text ({phantom_rate:.0%}); "
            f"threshold is {max_phantom_rate:.0%}"
        ),
    }


def gate_no_critical_circular_deps(
    findings: List[Dict[str, Any]],
    max_critical: int = 0,
) -> Dict[str, Any]:
    """Gate 4: No critical-severity circular dependencies.

    Critical circular deps = findings where the only sources are the subject's own
    disclosures and no independent verification exists.
    """
    critical_findings: List[Dict[str, Any]] = []
    high_findings: List[Dict[str, Any]] = []

    for f in findings:
        cd = f.get("circular_dep", {})
        severity = cd.get("severity", "none")
        if severity == "critical":
            critical_findings.append({
                "claim": f.get("claim", "")[:150],
                "severity": severity,
                "reason": cd.get("reason", ""),
            })
        elif severity == "high":
            high_findings.append({
                "claim": f.get("claim", "")[:150],
                "severity": severity,
                "reason": cd.get("reason", ""),
            })

    passed = len(critical_findings) <= max_critical

    return {
        "gate": "no_critical_circular_deps",
        "status": GateStatus.PASS.value if passed else GateStatus.FAIL.value,
        "threshold": max_critical,
        "critical_count": len(critical_findings),
        "high_count": len(high_findings),
        "critical_findings": critical_findings[:5],
        "message": (
            f"No critical circular dependencies detected"
            if passed else
            f"{len(critical_findings)} critical circular dep(s) found; "
            f"max allowed is {max_critical}"
        ),
    }


# ---------------------------------------------------------------------------
# Overall quality assessment
# ---------------------------------------------------------------------------

def run_all_gates(
    findings: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    verification: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all 4 quality gates and compute overall report status + quality score.

    Returns:
        {
            "status": "pass" | "pass_with_caveats" | "fail",
            "quality_score": int (0-100),
            "gates": [gate_result, ...],
            "gates_passed": int,
            "gates_total": 4,
            "finding_segmentation": {"core": n, "analysis": n, "unverified": n, "advocacy": n},
            "recommendations": [str],
        }
    """
    gates = [
        gate_verification_completeness(findings, verification),
        gate_citation_accuracy(verification),
        gate_no_phantom_citations(sources),
        gate_no_critical_circular_deps(findings),
    ]

    passed_count = sum(1 for g in gates if g["status"] == GateStatus.PASS.value)

    # Determine report status.
    if passed_count == 4:
        status = ReportStatus.PASS
    elif passed_count >= 3:
        status = ReportStatus.PASS_WITH_CAVEATS
    else:
        status = ReportStatus.FAIL

    # Compute quality score (0-100).
    quality_score = _compute_quality_score(gates, findings, sources, verification)

    # Finding segmentation counts.
    seg_counts = {"core": 0, "analysis": 0, "unverified": 0, "advocacy": 0}
    _type_to_seg = {"fact": "core", "analysis": "analysis", "interpretation": "unverified", "advocacy": "advocacy"}
    for f in findings:
        ft = f.get("finding_type", "analysis")
        seg = _type_to_seg.get(ft, "analysis")
        seg_counts[seg] += 1

    # Recommendations.
    recommendations = _generate_recommendations(gates, findings, sources)

    return {
        "status": status.value,
        "quality_score": quality_score,
        "gates": gates,
        "gates_passed": passed_count,
        "gates_total": 4,
        "finding_segmentation": seg_counts,
        "recommendations": recommendations,
    }


def _compute_quality_score(
    gates: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    verification: Dict[str, Any],
) -> int:
    """Weighted quality score from 0-100.

    Components:
      - Source quality (25%): tier distribution + retrieval rate
      - Verification (25%): faithfulness score
      - Citation accuracy (25%): coverage + accuracy
      - Gate compliance (25%): % of gates passed
    """
    # Source quality component (0-25).
    total_sources = len(sources) or 1
    retrieved = sum(
        1 for s in sources
        if (s.get("content") or "").strip() and len((s.get("content") or "").strip()) > 100
    )
    retrieval_rate = retrieved / total_sources
    source_score = retrieval_rate * 25

    # Verification component (0-25).
    faithfulness = verification.get("faithfulness_score", 0.0)
    verification_score = faithfulness * 25

    # Citation accuracy component (0-25).
    coverage = verification.get("citation_coverage", 0.0)
    accuracy_score = coverage * 25

    # Gate compliance (0-25).
    passed = sum(1 for g in gates if g["status"] == GateStatus.PASS.value)
    gate_score = (passed / 4) * 25

    total = source_score + verification_score + accuracy_score + gate_score
    return max(0, min(100, round(total)))


def _generate_recommendations(
    gates: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
) -> List[str]:
    """Generate actionable recommendations based on gate results."""
    recs: List[str] = []

    for g in gates:
        if g["status"] == GateStatus.FAIL.value:
            gate_name = g["gate"]
            if gate_name == "verification_completeness":
                recs.append(
                    f"Increase source verification: only {g['verified_findings']}/{g['total_findings']} "
                    f"findings are adequately verified. Re-scrape failed sources or find alternative sources."
                )
            elif gate_name == "citation_accuracy":
                recs.append(
                    f"Fix citation accuracy: {g['n_unsupported_claims']} claim(s) are not supported by "
                    f"their cited sources. Remove unsupported claims or find correct sources."
                )
            elif gate_name == "no_phantom_citations":
                phantom_urls = [p["url"] for p in g.get("phantom_sources", [])[:3]]
                recs.append(
                    f"Resolve phantom citations: {g['phantom_count']}/{g['total_sources']} sources have no "
                    f"retrievable text. Attempt re-retrieval or replace with accessible sources. "
                    f"Examples: {', '.join(phantom_urls)}"
                )
            elif gate_name == "no_critical_circular_deps":
                recs.append(
                    f"Address circular dependencies: {g['critical_count']} finding(s) rely solely on "
                    f"company self-disclosures. Add independent source verification or move to "
                    f"'unverified' section with disclaimer."
                )

    # Finding-type recommendations.
    advocacy_count = sum(1 for f in findings if f.get("finding_type") == "advocacy")
    if advocacy_count > 0:
        recs.append(
            f"{advocacy_count} finding(s) are advocacy-sourced and should be clearly labeled "
            f"as 'contested' with appropriate disclaimers in the report."
        )

    interp_count = sum(1 for f in findings if f.get("finding_type") == "interpretation")
    if interp_count > 0:
        recs.append(
            f"{interp_count} finding(s) contain interpretive/contested language that should be "
            f"reframed as compliance risk rather than moral judgment."
        )

    return recs
