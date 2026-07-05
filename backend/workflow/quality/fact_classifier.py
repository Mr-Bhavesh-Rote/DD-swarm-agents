"""Phase 2: Fact vs. interpretation classification.

Automatically classifies each finding as one of:
  - FACT:           Verifiable, specific claim (dates, amounts, official actions)
  - ANALYSIS:       Reasonable inference drawn from facts
  - INTERPRETATION: Contested characterization or moral judgment
  - ADVOCACY:       Sourced from an advocacy organization with a stated position

This classification drives report segmentation: core findings (facts + analysis)
vs. unverified/advocacy sections with appropriate disclaimers.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional

from workflow.quality.source_tiers import SourceTier, classify_source_tier


class FindingType(str, Enum):
    FACT = "fact"
    ANALYSIS = "analysis"
    INTERPRETATION = "interpretation"
    ADVOCACY = "advocacy"

    @property
    def section_label(self) -> str:
        return _SECTION_LABELS[self]


_SECTION_LABELS = {
    FindingType.FACT: "Core Findings (Verified)",
    FindingType.ANALYSIS: "Analytical Findings",
    FindingType.INTERPRETATION: "Contested / Interpretive Claims",
    FindingType.ADVOCACY: "Advocacy-Sourced Claims",
}


# ---------------------------------------------------------------------------
# Pattern-based scoring.  Each claim is scored against FACT, INTERPRETATION,
# and ANALYSIS patterns.  The highest-scoring category wins.
# ---------------------------------------------------------------------------

# Fact indicators: specific, verifiable data points.
_FACT_PATTERNS = [
    # Monetary amounts
    (re.compile(r"\$[\d,.]+\s*(million|billion|m|b|k|USD|EUR)?", re.I), 8),
    (re.compile(r"(USD|EUR|GBP)\s*[\d,.]+", re.I), 8),
    # Specific dates
    (re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}", re.I), 6),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), 6),
    (re.compile(r"\b(Q[1-4]|FY)\s*\d{4}\b", re.I), 5),
    # Official actions / legal language
    (re.compile(r"\b(fine[sd]?|penalt(y|ies)|settlement|convicted|indicted|charged|sentenced|sued|filed suit|enforcement action|consent (order|decree)|cease.and.desist)\b", re.I), 7),
    (re.compile(r"\b(court|tribunal|judge|jury|verdict|ruling|injunction)\b", re.I), 5),
    # Regulatory agencies
    (re.compile(r"\b(SEC|EPA|OSHA|OFAC|DOJ|FTC|HSE|EU Commission|ECHA|FDA|BIS|UN Security Council)\b"), 6),
    # Contract / procurement references
    (re.compile(r"\b(contract\s*(number|no\.?|#)|award|procurement|RFP|solicitation)\b", re.I), 5),
    # Specific percentages
    (re.compile(r"\b\d+(\.\d+)?%\b"), 4),
    # Named entity + specific action
    (re.compile(r"\b(announced|disclosed|reported|published|released|issued|granted|revoked|suspended)\b", re.I), 4),
]

# Interpretation indicators: contested characterizations, moral judgments.
_INTERPRETATION_PATTERNS = [
    # Nexus / complicity language
    (re.compile(r"\b(creates?\s+nexus\s+to|complicit\s+in|responsible\s+for\s+.*(?:crime|violation|abuse))", re.I), 10),
    (re.compile(r"\b(war\s+crime|crime\s+against\s+humanity|ethnic\s+cleansing|genocide)\b", re.I), 9),
    # Moral judgments
    (re.compile(r"\b(warrants?\s+concern|raises?\s+serious\s+(concern|question)|deeply\s+troubling|egregious)\b", re.I), 8),
    (re.compile(r"\b(should\s+be\s+(considered|viewed|seen)\s+as)\b", re.I), 7),
    # Speculative / unverified framing
    (re.compile(r"\b(likely|possibly|potentially|appears?\s+to|seems?\s+to|suggests?|implies?|indicative\s+of)\b", re.I), 5),
    # Advocacy framing
    (re.compile(r"\b(occupation|colonialism|apartheid|oppression|resistance)\b", re.I), 6),
    (re.compile(r"\b(solidarity|boycott|divest(ment)?|sanction\s+.*\s+movement)\b", re.I), 7),
]

# Analysis indicators: reasonable inference with analytical framing.
_ANALYSIS_PATTERNS = [
    (re.compile(r"\b(this\s+(creates?|represents?|indicates?|suggests?)|therefore|consequently|as\s+a\s+result)\b", re.I), 6),
    (re.compile(r"\b(risk\s+(exposure|factor|profile)|compliance\s+(risk|obligation|requirement))\b", re.I), 5),
    (re.compile(r"\b(reputational\s+risk|regulatory\s+risk|legal\s+risk|export\s+control\s+risk)\b", re.I), 5),
    (re.compile(r"\b(based\s+on|according\s+to|in\s+light\s+of|given\s+that)\b", re.I), 3),
]


def classify_finding(
    claim: str,
    source_urls: List[str],
    *,
    subject: str = "",
) -> Dict[str, Any]:
    """Classify a single finding claim.

    Returns:
        {
            "finding_type": "fact" | "analysis" | "interpretation" | "advocacy",
            "scores": {"fact": int, "analysis": int, "interpretation": int},
            "reason": str,
            "advocacy_source": bool,
        }
    """
    fact_score = 0
    interp_score = 0
    analysis_score = 0

    for pattern, weight in _FACT_PATTERNS:
        if pattern.search(claim):
            fact_score += weight

    for pattern, weight in _INTERPRETATION_PATTERNS:
        if pattern.search(claim):
            interp_score += weight

    for pattern, weight in _ANALYSIS_PATTERNS:
        if pattern.search(claim):
            analysis_score += weight

    # Check if any source is a Tier 4 advocacy org.
    advocacy_source = any(
        classify_source_tier(url) == SourceTier.TIER_4
        for url in source_urls
    )

    # Classification decision tree.
    if advocacy_source and interp_score >= fact_score:
        finding_type = FindingType.ADVOCACY
        reason = "Sourced from advocacy organization with interpretive framing"
    elif interp_score > fact_score and interp_score > analysis_score:
        finding_type = FindingType.INTERPRETATION
        reason = "Contains contested characterizations or moral judgments"
    elif analysis_score > fact_score and analysis_score > interp_score:
        finding_type = FindingType.ANALYSIS
        reason = "Analytical inference from underlying facts"
    elif fact_score > 0:
        finding_type = FindingType.FACT
        reason = "Contains specific, verifiable data points"
    elif advocacy_source:
        finding_type = FindingType.ADVOCACY
        reason = "Sourced from advocacy organization"
    else:
        finding_type = FindingType.ANALYSIS
        reason = "General claim without strong fact or interpretation markers"

    return {
        "finding_type": finding_type.value,
        "scores": {
            "fact": fact_score,
            "analysis": analysis_score,
            "interpretation": interp_score,
        },
        "reason": reason,
        "advocacy_source": advocacy_source,
    }


def classify_all_findings(
    findings: List[Dict[str, Any]],
    sources_by_id: Dict[int, Dict[str, Any]],
    subject: str = "",
) -> List[Dict[str, Any]]:
    """Classify all findings and return them with finding_type annotations.

    Each finding dict gets two new keys:
      - finding_type: "fact" | "analysis" | "interpretation" | "advocacy"
      - classification: full classification detail dict
    """
    result = []
    for f in findings:
        claim = f.get("claim", "")
        source_ids = f.get("source_ids", [])
        source_urls = [
            sources_by_id[sid].get("url", "")
            for sid in source_ids
            if sid in sources_by_id
        ]
        classification = classify_finding(claim, source_urls, subject=subject)
        enriched = dict(f)
        enriched["finding_type"] = classification["finding_type"]
        enriched["classification"] = classification
        result.append(enriched)
    return result


def segment_findings(
    findings: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Segment classified findings into buckets for report generation.

    Returns:
        {
            "core": [...]          # FACT findings
            "analysis": [...]      # ANALYSIS findings
            "unverified": [...]    # INTERPRETATION findings
            "advocacy": [...]      # ADVOCACY findings
        }
    """
    segments: Dict[str, List[Dict[str, Any]]] = {
        "core": [],
        "analysis": [],
        "unverified": [],
        "advocacy": [],
    }
    _type_to_segment = {
        "fact": "core",
        "analysis": "analysis",
        "interpretation": "unverified",
        "advocacy": "advocacy",
    }
    for f in findings:
        ft = f.get("finding_type", "analysis")
        segment = _type_to_segment.get(ft, "analysis")
        segments[segment].append(f)
    return segments
