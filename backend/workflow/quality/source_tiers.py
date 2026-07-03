"""Phase 1: Source quality hierarchy & confidence calculation.

Assigns a credibility tier (1-4) to each source based on its publisher/URL domain,
tracks retrievability state, and computes a confidence score for each finding based
on the quality and retrievability of its supporting sources.

Tier definitions:
  TIER_1: Government/official records (SEC, treasury.gov, un.org, court records)
  TIER_2: Major credible news organizations (Reuters, AP, NYT, BBC, etc.)
  TIER_3: Investigative journalism & established NGOs (HRW, Amnesty, OCCRP, etc.)
  TIER_4: Advocacy organizations with a stated position against the subject
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


class SourceTier(IntEnum):
    TIER_1 = 1  # Government / official records
    TIER_2 = 2  # Major credible news
    TIER_3 = 3  # Investigative journalism / established NGOs
    TIER_4 = 4  # Advocacy organizations

    @property
    def label(self) -> str:
        return _TIER_LABELS[self]


_TIER_LABELS = {
    SourceTier.TIER_1: "Government / Official Record",
    SourceTier.TIER_2: "Major News Organization",
    SourceTier.TIER_3: "Investigative / NGO",
    SourceTier.TIER_4: "Advocacy Organization",
}


class Retrievability(IntEnum):
    RETRIEVED = 1
    PARTIALLY_RETRIEVED = 2
    NOT_RETRIEVED = 3
    NOT_ATTEMPTED = 4

    @property
    def label(self) -> str:
        return _RETR_LABELS[self]


_RETR_LABELS = {
    Retrievability.RETRIEVED: "Full text retrieved",
    Retrievability.PARTIALLY_RETRIEVED: "Partial text only",
    Retrievability.NOT_RETRIEVED: "Retrieval failed",
    Retrievability.NOT_ATTEMPTED: "Not attempted",
}


# ---------------------------------------------------------------------------
# Domain -> tier mapping.  Checked longest-suffix-first so e.g. "echo.epa.gov"
# matches before the generic ".gov" rule.
# ---------------------------------------------------------------------------
_TIER_1_DOMAINS = {
    # US Government
    "sec.gov", "treasury.gov", "home.treasury.gov", "ofac.treasury.gov",
    "commerce.gov", "bis.doc.gov", "state.gov", "justice.gov",
    "epa.gov", "echo.epa.gov", "osha.gov", "ftc.gov", "fbi.gov",
    "fpds.gov", "usaspending.gov", "pacer.uscourts.gov",
    "courtlistener.com",
    # International government / multilateral
    "un.org", "europa.eu", "sanctionsmap.eu", "gov.uk", "gov.il",
    "echa.europa.eu",
    # Stock exchanges / regulators
    "sec.report", "efts.sec.gov",
}

_TIER_2_DOMAINS = {
    # Wire services
    "reuters.com", "apnews.com", "afp.com",
    # Major newspapers
    "nytimes.com", "washingtonpost.com", "wsj.com", "ft.com",
    "theguardian.com", "bbc.com", "bbc.co.uk",
    "economist.com", "bloomberg.com", "aljazeera.com",
    "haaretz.com", "timesofisrael.com", "jpost.com",
    "aa.com.tr",  # Anadolu Agency
    # Major business / trade
    "forbes.com", "cnbc.com", "businessinsider.com",
}

_TIER_3_DOMAINS = {
    # Investigative journalism
    "occrp.org", "icij.org", "propublica.org",
    # Established human rights / watchdog NGOs
    "hrw.org", "amnesty.org", "transparency.org",
    "globalwitness.org", "bellona.org",
    # Compliance / research databases
    "goodjobsfirst.org",  # Violation Tracker
    "opensecrets.org",
    "whoprofits.org",  # NOTE: could be Tier 3 or 4 depending on context
}

_TIER_4_DOMAINS = {
    # Advocacy organizations with stated opposition to subjects
    "aseed.net", "bdsmovement.net", "waronwant.org",
    "electronicintifada.net", "mondoweiss.net",
    "corporatewatch.org",
}


def classify_source_tier(url: str, publisher: str = "") -> SourceTier:
    """Classify a source URL into a credibility tier."""
    host = _extract_host(url)
    if not host:
        return SourceTier.TIER_4  # unknown = lowest tier

    # Check exact domain matches (most specific first).
    for domain in _TIER_1_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return SourceTier.TIER_1
    for domain in _TIER_2_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return SourceTier.TIER_2
    for domain in _TIER_3_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return SourceTier.TIER_3
    for domain in _TIER_4_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return SourceTier.TIER_4

    # Heuristic fallbacks by TLD.
    if host.endswith(".gov") or host.endswith(".mil"):
        return SourceTier.TIER_1
    if host.endswith(".edu"):
        return SourceTier.TIER_2
    if host.endswith(".org"):
        return SourceTier.TIER_3

    # Company's own website (investor relations, corporate site).
    return SourceTier.TIER_3  # default: treat as investigative / unknown


def classify_retrievability(source: Dict[str, Any]) -> Retrievability:
    """Determine the retrievability state of a source record."""
    content = (source.get("content") or "").strip()
    snippet = (source.get("snippet") or "").strip()
    if content and len(content) > 200:
        return Retrievability.RETRIEVED
    if content or (snippet and len(snippet) > 50):
        return Retrievability.PARTIALLY_RETRIEVED
    # If the source has a content_hash but empty content, retrieval was attempted but failed.
    if source.get("content_hash") and not content:
        return Retrievability.NOT_RETRIEVED
    return Retrievability.NOT_ATTEMPTED


def is_company_source(url: str, subject: str) -> bool:
    """Check if a URL is the subject company's own website (self-citation)."""
    host = _extract_host(url)
    if not host:
        return False
    # Normalize the subject name for matching.
    subject_parts = re.split(r"[\s\-.,()]+", subject.lower())
    # Filter out common words.
    stop = {"ltd", "inc", "corp", "group", "plc", "co", "company", "the", "of", "and", ""}
    subject_tokens = [p for p in subject_parts if p not in stop and len(p) > 2]
    # Check if multiple subject name tokens appear in the hostname.
    matches = sum(1 for t in subject_tokens if t in host)
    return matches >= 1 and not any(
        host.endswith(d) for d in (".gov", ".edu", ".org")
        if d not in (".org",)  # .org can be a company domain
    )


# ---------------------------------------------------------------------------
# Confidence calculator
# ---------------------------------------------------------------------------
_TIER_POINTS = {
    SourceTier.TIER_1: 40,
    SourceTier.TIER_2: 30,
    SourceTier.TIER_3: 15,
    SourceTier.TIER_4: 5,
}

_RETR_MULTIPLIER = {
    Retrievability.RETRIEVED: 1.0,
    Retrievability.PARTIALLY_RETRIEVED: 0.6,
    Retrievability.NOT_RETRIEVED: 0.1,
    Retrievability.NOT_ATTEMPTED: 0.0,
}

# Confidence level thresholds (out of 100).
CONFIDENCE_HIGH = 70
CONFIDENCE_MEDIUM_HIGH = 50
CONFIDENCE_MEDIUM = 30
CONFIDENCE_LOW_MEDIUM = 15


def compute_finding_confidence(
    source_records: List[Dict[str, Any]],
    subject: str,
    *,
    has_circular_dep: bool = False,
) -> Dict[str, Any]:
    """Compute a confidence score (0-100) and level for a finding based on its sources.

    Returns:
        {
            "score": int,
            "level": "high" | "medium_high" | "medium" | "low_medium" | "low",
            "source_tiers": [{source_id, tier, retrievability, is_company_source}],
            "penalties": [str],
        }
    """
    if not source_records:
        return {
            "score": 0, "level": "low",
            "source_tiers": [], "penalties": ["No sources provided"],
        }

    total_points = 0
    penalties: List[str] = []
    source_assessments: List[Dict[str, Any]] = []
    independent_count = 0
    company_count = 0

    for src in source_records:
        tier = classify_source_tier(src.get("url", ""))
        retr = classify_retrievability(src)
        is_company = is_company_source(src.get("url", ""), subject)

        base_points = _TIER_POINTS[tier]
        multiplier = _RETR_MULTIPLIER[retr]
        points = base_points * multiplier

        if is_company:
            company_count += 1
            points *= 0.3  # company sources heavily discounted
        else:
            independent_count += 1

        total_points += points
        source_assessments.append({
            "source_id": src.get("id"),
            "url": src.get("url", ""),
            "tier": tier.value,
            "tier_label": tier.label,
            "retrievability": retr.value,
            "retrievability_label": retr.label,
            "is_company_source": is_company,
            "points": round(points, 1),
        })

    # Penalties.
    unretrieved = sum(
        1 for a in source_assessments
        if a["retrievability"] >= Retrievability.NOT_RETRIEVED
    )
    if unretrieved:
        penalty = unretrieved * 20
        total_points -= penalty
        penalties.append(f"-{penalty} pts: {unretrieved} source(s) not retrieved")

    if has_circular_dep:
        total_points -= 15
        penalties.append("-15 pts: circular dependency detected")

    if independent_count == 0 and company_count > 0:
        total_points -= 25
        penalties.append("-25 pts: only company self-citations, no independent sources")

    # Clamp to 0-100.
    score = max(0, min(100, int(total_points)))

    # Map to level.
    if score >= CONFIDENCE_HIGH:
        level = "high"
    elif score >= CONFIDENCE_MEDIUM_HIGH:
        level = "medium_high"
    elif score >= CONFIDENCE_MEDIUM:
        level = "medium"
    elif score >= CONFIDENCE_LOW_MEDIUM:
        level = "low_medium"
    else:
        level = "low"

    return {
        "score": score,
        "level": level,
        "source_tiers": source_assessments,
        "penalties": penalties,
    }


def assess_all_sources(sources: List[Dict[str, Any]], subject: str) -> Dict[str, Any]:
    """Produce a source quality summary for the entire source registry.

    Returns:
        {
            "total_sources": int,
            "tier_distribution": {1: n, 2: n, 3: n, 4: n},
            "retrievability_distribution": {1: n, 2: n, 3: n, 4: n},
            "company_sources": int,
            "independent_sources": int,
            "retrieval_rate": float,
            "per_source": [{id, url, tier, retrievability, is_company_source}],
        }
    """
    tier_dist = {1: 0, 2: 0, 3: 0, 4: 0}
    retr_dist = {1: 0, 2: 0, 3: 0, 4: 0}
    company = 0
    independent = 0
    per_source: List[Dict[str, Any]] = []

    for s in sources:
        tier = classify_source_tier(s.get("url", ""))
        retr = classify_retrievability(s)
        is_co = is_company_source(s.get("url", ""), subject)
        tier_dist[tier.value] += 1
        retr_dist[retr.value] += 1
        if is_co:
            company += 1
        else:
            independent += 1
        per_source.append({
            "id": s.get("id"),
            "url": s.get("url", ""),
            "tier": tier.value,
            "tier_label": tier.label,
            "retrievability": retr.value,
            "retrievability_label": retr.label,
            "is_company_source": is_co,
        })

    retrieved = retr_dist.get(1, 0) + retr_dist.get(2, 0)
    total = len(sources) or 1

    return {
        "total_sources": len(sources),
        "tier_distribution": tier_dist,
        "retrievability_distribution": retr_dist,
        "company_sources": company,
        "independent_sources": independent,
        "retrieval_rate": round(retrieved / total, 4),
        "per_source": per_source,
    }


def _extract_host(url: str) -> str:
    try:
        host = urlsplit(url.strip()).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""
