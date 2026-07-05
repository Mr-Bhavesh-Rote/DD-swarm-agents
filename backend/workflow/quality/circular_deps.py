"""Phase 3: Circular dependency detection.

Detects when a finding about a subject is corroborated only by the subject's own
disclosures (SEC filings, corporate website, investor relations, press releases).
This is a circular dependency: the company's own claims should not be treated as
independent corroboration of adverse findings about the company.

Also detects when advocacy sources are the sole basis for a claim that is then
"corroborated" by the advocacy organization's own publications.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List

from workflow.quality.source_tiers import (
    SourceTier,
    classify_source_tier,
    is_company_source,
)


class CircularSeverity(str, Enum):
    CRITICAL = "critical"   # Only company sources, no independent verification
    HIGH = "high"           # Majority company sources, minimal independent
    MEDIUM = "medium"       # Some company sources used as corroboration
    LOW = "low"             # Minor circular pattern, adequate independent sources
    NONE = "none"           # No circular dependency detected


class CircularAction(str, Enum):
    REMOVE = "remove"       # Remove from core findings
    REVISE = "revise"       # Revise to note limited independent verification
    FLAG = "flag"           # Flag but include with disclaimer
    INCLUDE = "include"     # No action needed


def detect_circular_dependency(
    finding: Dict[str, Any],
    sources_by_id: Dict[int, Dict[str, Any]],
    subject: str,
) -> Dict[str, Any]:
    """Analyze a single finding for circular dependency patterns.

    Returns:
        {
            "has_circular_dep": bool,
            "severity": "critical" | "high" | "medium" | "low" | "none",
            "recommended_action": "remove" | "revise" | "flag" | "include",
            "company_sources": [source_ids],
            "independent_sources": [source_ids],
            "advocacy_only_sources": [source_ids],
            "reason": str,
        }
    """
    source_ids = finding.get("source_ids", [])
    if not source_ids:
        return {
            "has_circular_dep": False,
            "severity": CircularSeverity.NONE.value,
            "recommended_action": CircularAction.FLAG.value,
            "company_sources": [],
            "independent_sources": [],
            "advocacy_only_sources": [],
            "reason": "No sources to evaluate",
        }

    company_sids: List[int] = []
    independent_sids: List[int] = []
    advocacy_sids: List[int] = []

    for sid in source_ids:
        src = sources_by_id.get(sid, {})
        url = src.get("url", "")
        if is_company_source(url, subject):
            company_sids.append(sid)
        elif classify_source_tier(url) == SourceTier.TIER_4:
            advocacy_sids.append(sid)
        else:
            independent_sids.append(sid)

    total = len(source_ids)
    n_company = len(company_sids)
    n_independent = len(independent_sids)
    n_advocacy = len(advocacy_sids)

    # Determine severity and action.
    if n_independent == 0 and n_company > 0 and n_advocacy == 0:
        # Only company sources: critical circular dependency.
        severity = CircularSeverity.CRITICAL
        action = CircularAction.REVISE
        reason = (
            f"All {n_company} source(s) are from the subject's own disclosures. "
            "No independent verification available."
        )
    elif n_independent == 0 and n_advocacy > 0 and n_company == 0:
        # Only advocacy sources: not circular but weak.
        severity = CircularSeverity.HIGH
        action = CircularAction.FLAG
        reason = (
            f"All {n_advocacy} source(s) are from advocacy organizations. "
            "No independent or official verification available."
        )
    elif n_independent == 0 and n_company > 0 and n_advocacy > 0:
        # Company + advocacy only: circular + advocacy bias.
        severity = CircularSeverity.CRITICAL
        action = CircularAction.REVISE
        reason = (
            f"Sources are {n_company} company disclosure(s) + {n_advocacy} advocacy org(s). "
            "Company disclosures cannot independently corroborate advocacy claims."
        )
    elif n_company > n_independent and n_independent > 0:
        # Majority company sources.
        severity = CircularSeverity.MEDIUM
        action = CircularAction.FLAG
        reason = (
            f"{n_company}/{total} sources are company disclosures; "
            f"only {n_independent} independent source(s)."
        )
    elif n_company > 0 and n_independent >= n_company:
        # Some company sources, but adequate independent corroboration.
        severity = CircularSeverity.LOW
        action = CircularAction.INCLUDE
        reason = (
            f"{n_company} company source(s) present but {n_independent} independent source(s) "
            "provide adequate corroboration."
        )
    else:
        severity = CircularSeverity.NONE
        action = CircularAction.INCLUDE
        reason = "No circular dependency detected."

    return {
        "has_circular_dep": severity in (CircularSeverity.CRITICAL, CircularSeverity.HIGH, CircularSeverity.MEDIUM),
        "severity": severity.value,
        "recommended_action": action.value,
        "company_sources": company_sids,
        "independent_sources": independent_sids,
        "advocacy_only_sources": advocacy_sids,
        "reason": reason,
    }


def detect_all_circular_deps(
    findings: List[Dict[str, Any]],
    sources_by_id: Dict[int, Dict[str, Any]],
    subject: str,
) -> List[Dict[str, Any]]:
    """Analyze all findings for circular dependencies.

    Returns findings enriched with a `circular_dep` key containing the analysis.
    """
    result = []
    for f in findings:
        analysis = detect_circular_dependency(f, sources_by_id, subject)
        enriched = dict(f)
        enriched["circular_dep"] = analysis
        result.append(enriched)
    return result


def circular_dep_summary(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce a summary of circular dependency findings.

    Returns:
        {
            "total_findings": int,
            "findings_with_circular_deps": int,
            "severity_distribution": {"critical": n, "high": n, "medium": n, "low": n, "none": n},
            "action_distribution": {"remove": n, "revise": n, "flag": n, "include": n},
        }
    """
    sev_dist = {"critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0}
    act_dist = {"remove": 0, "revise": 0, "flag": 0, "include": 0}
    circular_count = 0

    for f in findings:
        cd = f.get("circular_dep", {})
        sev = cd.get("severity", "none")
        act = cd.get("recommended_action", "include")
        sev_dist[sev] = sev_dist.get(sev, 0) + 1
        act_dist[act] = act_dist.get(act, 0) + 1
        if cd.get("has_circular_dep"):
            circular_count += 1

    return {
        "total_findings": len(findings),
        "findings_with_circular_deps": circular_count,
        "severity_distribution": sev_dist,
        "action_distribution": act_dist,
    }
