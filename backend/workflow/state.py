"""LangGraph state schema (§4.1).

Research branches run in parallel via `Send` and must only *append* to reducer-merged
channels (`findings`, `raw_outputs`, `sources_raw`) — never write a shared scalar — to
avoid LangGraph concurrent-write errors. Scalar channels (plan, final_report, ...) are
written by single nodes only.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def _merge_dicts(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(left or {})
    out.update(right or {})
    return out


class GraphState(TypedDict, total=False):
    # --- inputs (set once at invocation) ---
    run_id: str
    subject: str
    subject_type: str
    task: str
    model_config: Dict[str, Any]   # {global_default, role_overrides}
    plan_override: Optional[Dict[str, Any]]
    uploaded_file_ids: List[str]

    # --- planner output ---
    plan: Dict[str, Any]           # normalized WorkflowPlan dict

    # --- parallel research swarm outputs (reducer-merged; append-only) ---
    raw_outputs: Annotated[List[Dict[str, Any]], operator.add]   # AgentOutput dicts
    findings: Annotated[List[Dict[str, Any]], operator.add]      # raw findings w/ source_urls
    sources_raw: Annotated[List[Dict[str, Any]], operator.add]   # {url,title,publisher,content,...}

    # --- aggregator output ---
    aggregated_findings: List[Dict[str, Any]]   # Finding dicts (deduped, source_ids assigned)
    sources: List[Dict[str, Any]]               # global Source[] (citation_id assigned)
    buckets: List[Dict[str, Any]]

    # --- synthesizer / verifier loop ---
    draft_sections: List[Dict[str, Any]]
    verification: Dict[str, Any]
    revision_count: int
    needs_revision: bool

    # --- final artifacts ---
    raw_report: Dict[str, Any]
    final_report: Dict[str, Any]

    # --- bookkeeping ---
    model_summary: Annotated[Dict[str, str], _merge_dicts]
    cost_usd: Annotated[float, operator.add]
    events: Annotated[List[Dict[str, Any]], operator.add]
