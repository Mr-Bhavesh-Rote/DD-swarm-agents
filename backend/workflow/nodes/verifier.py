"""verifier node (§4.1 node 5, §4.2).

For each cited claim, run an LLM-as-judge faithfulness check against the cited source's
stored text. Compute a citation coverage score and a faithfulness score. Unsupported
citations are flagged; if any claim fails and the revision budget remains, route back to
the synthesizer. Both scores are written to the run and pushed to Langfuse as eval scores
by the API layer.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any, Dict, List, Tuple

from app.core.config import get_settings
from app.core.prompts import build_verifier_prompt
from workflow.llm import extract_list, invoke_json
from workflow.models import resolve_model

# Verify at most this many claims per LLM call so the JSON response stays within
# max_tokens and remains parseable.
_VERIFY_BATCH = 8

_CITE_RE = re.compile(r"\[(\d+)\]")


def _norm_claim(text: str) -> str:
    """Normalize claim text for tolerant verdict matching (drop [n] markers + whitespace)."""
    return " ".join(_CITE_RE.sub("", text or "").lower().split())
# Split body into sentence-ish statements for coverage accounting.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_UNVERIFIED_RE = re.compile(r"\[(unverified|estimate)\]", re.IGNORECASE)


def verifier_node(state: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    settings = get_settings()
    model_config = state.get("model_config", {})
    callbacks = (config or {}).get("callbacks")
    verifier_model = resolve_model(role="verifier", model_config=model_config)

    sections = state.get("draft_sections", [])
    sources_by_id = {s["id"]: s for s in state.get("sources", [])}

    # 1. Extract cited claims from every section.
    claims = _extract_claims(sections)

    # 2. Coverage accounting over all statements. An empty/contentless report (no countable
    #    statements) is a FAILURE, not a vacuous pass — score it 0 so a blank report never
    #    shows up as 100% coverage (which historically masked silent synthesis failures).
    total_statements, cited_statements, labelled = _coverage_counts(sections)
    coverage = (cited_statements + labelled) / total_statements if total_statements else 0.0

    # 3. LLM-as-judge faithfulness for each cited claim. Verify in small batches so the
    #    JSON response never truncates (one big call over all claims overruns max_tokens
    #    and produces unparseable output -> every claim would default to unsupported).
    flags: List[Dict[str, Any]] = []
    supported = 0
    cost = 0.0
    if claims:
        sys = build_verifier_prompt()
        verdicts: Dict[int, Dict[str, Any]] = {}
        verdicts_by_text: Dict[str, Dict[str, Any]] = {}

        # Verify batches CONCURRENTLY — they're independent, and running them one-by-one was
        # a major latency sink (each Opus batch ~50s). A thread pool overlaps the network I/O;
        # contextvars are copied so the Langfuse trace context propagates into each thread.
        batches = [(start, claims[start : start + _VERIFY_BATCH])
                   for start in range(0, len(claims), _VERIFY_BATCH)]

        def _run_batch(start: int, batch: List[Dict[str, Any]]):
            payload = _build_verifier_payload(batch, sources_by_id)
            return start, batch, invoke_json(
                verifier_model, sys, payload, callbacks=callbacks,
                max_tokens=settings.verifier_max_tokens,
            )

        max_workers = min(len(batches), 6)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(copy_context().run, _run_batch, s, b) for s, b in batches]
            for fut in as_completed(futures):
                start, batch, result = fut.result()
                cost += result["cost_usd"]
                for r in extract_list(result["data"], "results"):
                    # Tolerant matching: accept an explicit integer claim_index, and also key
                    # by echoed claim text (robust to verifier-prompt version differences).
                    ci = r.get("claim_index")
                    if isinstance(ci, int) and 0 <= ci < len(batch):
                        verdicts[start + ci] = r
                    if r.get("claim"):
                        verdicts_by_text[_norm_claim(r["claim"])] = r

        for i, c in enumerate(claims):
            verdict = verdicts.get(i) or verdicts_by_text.get(_norm_claim(c["text"]))
            is_supported = bool(verdict.get("supported")) if verdict else False
            if is_supported:
                supported += 1
            else:
                flags.append({
                    "section_id": c["section_id"],
                    "claim": c["text"],
                    "citation_ids": c["citation_ids"],
                    "reason": (verdict or {}).get("reason", "No supporting source text found."),
                    "status": "unsupported",
                })

    # No cited claims is only "perfect" if the report actually has content; a blank report
    # (no statements at all) scores 0 rather than a misleading 100%.
    faithfulness = (supported / len(claims)) if claims else (1.0 if total_statements else 0.0)

    verification = {
        "citation_coverage": round(coverage, 4),
        "faithfulness_score": round(faithfulness, 4),
        "flags": flags,
    }

    revision_count = state.get("revision_count", 0)
    # Only revise when faithfulness is genuinely poor — a near-clean report shouldn't pay for
    # a full re-synthesis over a few weak citations (those are recorded in flags instead).
    needs_revision = (
        bool(flags)
        and faithfulness < settings.revision_min_faithfulness
        and revision_count < settings.max_revisions
    )

    return {
        "verification": verification,
        "revision_count": revision_count + (1 if needs_revision else 0),
        "needs_revision": needs_revision,
        "cost_usd": cost,
        "model_summary": {"verifier": verifier_model},
        "events": [{"node": "verifier", "status": "completed",
                    "citation_coverage": verification["citation_coverage"],
                    "faithfulness_score": verification["faithfulness_score"],
                    "n_flags": len(flags), "needs_revision": needs_revision}],
    }


def route_after_verify(state: Dict[str, Any]) -> str:
    """Conditional edge: revise (back to synthesizer) or finalize (renderer)."""
    return "synthesizer" if state.get("needs_revision") else "renderer"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _extract_claims(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    for sec in sections:
        body = sec.get("body_markdown", "") or ""
        for sentence in _SENT_SPLIT.split(body):
            ids = [int(x) for x in _CITE_RE.findall(sentence)]
            if ids:
                claims.append({
                    "section_id": sec["id"],
                    "text": _CITE_RE.sub("", sentence).strip(),
                    "citation_ids": sorted(set(ids)),
                })
    return claims


_SKIP_RE = re.compile(
    r"^(\s*\|)"                # table rows
    r"|^(\s*#{1,6}\s)"         # markdown headers
    r"|^(\s*[-*]\s*$)"         # list markers with no content
    r"|^(\s*---)"              # horizontal rules / table separators
    r"|^(\*\*[^*]+\*\*\s*$)"   # bold-only labels (e.g. "**Legal name:**")
)


def _coverage_counts(sections: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    total = cited = labelled = 0
    for sec in sections:
        body = sec.get("body_markdown", "") or ""
        for sentence in _SENT_SPLIT.split(body):
            s = sentence.strip()
            if len(s) < 12:  # skip fragments
                continue
            if _SKIP_RE.match(s):  # skip non-prose elements
                continue
            total += 1
            if _CITE_RE.search(s):
                cited += 1
            elif _UNVERIFIED_RE.search(s):
                labelled += 1
    return total, cited, labelled


def _build_verifier_payload(claims: List[Dict[str, Any]], sources_by_id: Dict[int, Dict[str, Any]]) -> str:
    lines: List[str] = [
        "Verify each of the following claims against its cited source text. "
        "Return one result per claim index.\n"
        "IMPORTANT: If a source has NO TEXT (marked [NO SOURCE TEXT AVAILABLE]), you MUST "
        "mark the claim as supported=false with reason 'Source text not retrievable'. "
        "Do NOT guess or assume the source supports the claim.\n"
    ]
    cap = get_settings().verifier_source_chars
    for i, c in enumerate(claims):
        lines.append(f"CLAIM {i} [section={c['section_id']}]: {c['text']}")
        for cid in c["citation_ids"]:
            src = sources_by_id.get(cid, {})
            text = (src.get("content") or src.get("snippet") or "").strip()[:cap]
            if text:
                lines.append(f"  SOURCE [{cid}] {src.get('url','')}:\n  {text}")
            else:
                lines.append(f"  SOURCE [{cid}] {src.get('url','')}: [NO SOURCE TEXT AVAILABLE]")
        lines.append("")
    return "\n".join(lines)
