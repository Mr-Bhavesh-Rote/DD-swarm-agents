"""aggregator node (§4.1 node 3).

Builds the global citation registry (dedupe by canonical URL, assign stable [n] ids),
resolves each finding's source_urls -> source_ids, deduplicates findings, and buckets
them: risk categories+severity for `company`, bio/career/investments/financial-legal
for `individual`. The bucketing is produced by the aggregator model; the citation
registry and dedup are deterministic code (no fabrication).
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.prompts import build_aggregator_prompt
from workflow.citations import CitationRegistry
from workflow.llm import invoke_json
from workflow.models import resolve_model


def aggregator_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    subject = state["subject"]
    subject_type = state["subject_type"]
    model_config = state.get("model_config", {})
    callbacks = (config or {}).get("callbacks")

    # 1. Build the global citation registry from every fetched source.
    registry = CitationRegistry()
    for s in state.get("sources_raw", []):
        registry.add(
            s.get("url", ""),
            title=s.get("title", ""),
            publisher=s.get("publisher", ""),
            snippet=s.get("snippet", ""),
            content=s.get("content", ""),
            retrieved_at=s.get("retrieved_at"),
        )

    # 2. Resolve finding source_urls -> source_ids; register any URL not yet seen.
    resolved: List[Dict[str, Any]] = []
    for f in state.get("findings", []):
        source_ids = []
        for url in f.get("source_urls", []) or []:
            cid = registry.id_for_url(url) or registry.add(url)
            if cid:
                source_ids.append(cid)
        resolved.append({
            "agent": f.get("agent", ""),
            "claim": (f.get("claim", "") or "").strip(),
            "source_ids": sorted(set(source_ids)),
            "confidence": f.get("confidence", "medium"),
            "category": f.get("category"),
        })

    # 3. Deterministic dedup by (normalized claim text).
    deduped = _dedup_findings(resolved)

    # 4. Model-driven bucketing/severity over the deduped set.
    buckets: List[Dict[str, Any]] = []
    cost = 0.0
    agg_model = resolve_model(role="aggregator", model_config=model_config)
    if deduped:
        sys = build_aggregator_prompt(subject, subject_type)
        enumerated = "\n".join(
            f"[{i}] ({f['category'] or 'uncategorized'}) {f['claim']}" for i, f in enumerate(deduped)
        )
        try:
            result = invoke_json(agg_model, sys, enumerated, callbacks=callbacks, max_tokens=3000)
            cost = result["cost_usd"]
            raw_buckets = (result["data"] or {}).get("buckets", [])
            for b in raw_buckets:
                idxs = [i for i in b.get("finding_indexes", []) if isinstance(i, int) and 0 <= i < len(deduped)]
                # Stamp category/severity back onto findings.
                for i in idxs:
                    if b.get("category"):
                        deduped[i]["category"] = b["category"]
                buckets.append({
                    "category": b.get("category", "Uncategorized"),
                    "severity": b.get("severity"),
                    "finding_indexes": idxs,
                })
        except Exception:
            buckets = []

    return {
        "aggregated_findings": deduped,
        "sources": registry.sources(include_content=True),
        "buckets": buckets,
        "cost_usd": cost,
        "model_summary": {"aggregator": agg_model},
        "events": [{"node": "aggregator", "status": "completed",
                    "n_findings": len(deduped), "n_sources": len(registry.sources()),
                    "n_buckets": len(buckets)}],
    }


def _dedup_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for f in findings:
        key = " ".join(f["claim"].lower().split())
        if not key:
            continue
        if key in seen:
            # Merge source ids; keep highest confidence.
            existing = seen[key]
            existing["source_ids"] = sorted(set(existing["source_ids"]) | set(f["source_ids"]))
            if _conf_rank(f["confidence"]) > _conf_rank(existing["confidence"]):
                existing["confidence"] = f["confidence"]
        else:
            seen[key] = dict(f)
    return list(seen.values())


def _conf_rank(c: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(c, 1)
