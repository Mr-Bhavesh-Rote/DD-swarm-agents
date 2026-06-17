"""Unit tests for the headless workflow core (no network / no LLM)."""
from __future__ import annotations

import pytest

from app.schemas.contracts import RunRequest
from workflow.citations import CitationRegistry, canonical_url
from workflow.config_loader import ConfigError, load_plan_for_subject, normalize_plan
from workflow.models import resolve_model
from workflow.nodes.verifier import _coverage_counts, _extract_claims


def test_canonical_url_dedup():
    # Dedup normalizes scheme/host case, trailing slash and fragment (paths stay
    # case-sensitive per RFC 3986).
    r = CitationRegistry()
    a = r.add("https://Example.com/path/#frag", title="t", content="hello")
    b = r.add("https://example.com/path", title="", content="")
    assert a == b == 1
    assert canonical_url("https://A.com/x/") == "https://a.com/x"


def test_citation_ids_stable_in_order():
    r = CitationRegistry()
    assert r.add("https://a.com") == 1
    assert r.add("https://b.com") == 2
    assert r.add("https://a.com") == 1  # dedup keeps first id


def test_config_loader_company_and_individual():
    for st, n in (("company", 6), ("individual", 5)):
        plan = load_plan_for_subject(st)
        assert len(plan.agents) == n
        assert plan.research_agents()  # swarm non-empty


def test_config_loader_rejects_cycle():
    bad = {
        "agents": [
            {"name": "a", "role": "", "goal": "", "depends_on": ["b"]},
            {"name": "b", "role": "", "goal": "", "depends_on": ["a"]},
        ]
    }
    with pytest.raises(ConfigError):
        normalize_plan(bad)


def test_config_loader_rejects_unknown_tool():
    bad = {"agents": [{"name": "a", "role": "", "goal": "", "suggested_tools": ["nope"]}]}
    with pytest.raises(ConfigError):
        normalize_plan(bad)


def test_model_resolution_precedence():
    # per-agent override wins
    assert resolve_model(role="research", model_config={"global_default": "g"}, agent_model="claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    # per-role override beats global
    assert resolve_model(role="research", model_config={"role_overrides": {"research": "claude-sonnet-4-6"}, "global_default": "g"}) == "claude-sonnet-4-6"
    # global beats system default
    assert resolve_model(role="writer", model_config={"global_default": "claude-fable-5"}) == "claude-fable-5"


def test_verifier_claim_extraction_and_coverage():
    sections = [
        {"id": "s1", "title": "S1", "body_markdown": "Acme was founded in 2016 [1][2]. It is large."},
    ]
    claims = _extract_claims(sections)
    assert len(claims) == 1
    assert claims[0]["citation_ids"] == [1, 2]
    total, cited, labelled = _coverage_counts(sections)
    assert total == 2 and cited == 1


def test_extract_list_tolerates_shape():
    from workflow.llm import extract_list

    assert extract_list({"buckets": [1, 2]}, "buckets") == [1, 2]
    assert extract_list([{"a": 1}], "buckets") == [{"a": 1}]  # model returned a bare array
    assert extract_list({"x": 1}, "buckets") == []
    assert extract_list(None, "buckets") == []
    assert extract_list("oops", "buckets") == []


def test_research_finalize_tolerates_list_output():
    # A model returning a JSON array (not the agreed object) must not crash _finalize.
    from workflow.nodes.research import _finalize
    from workflow.tools import ToolContext
    from app.schemas.contracts import AgentSpec

    spec = AgentSpec(name="r", role="R", goal="g")
    ctx = ToolContext()

    # bare list of finding dicts
    out = _finalize(spec, "claude-sonnet-4-6", [{"claim": "x", "source_urls": ["u"]}], ctx, 0.0)
    assert out["findings"] and out["findings"][0]["claim"] == "x"
    # list wrapping the object
    out = _finalize(spec, "claude-sonnet-4-6", [{"narrative_markdown": "hi", "findings": []}], ctx, 0.0)
    assert out["raw_outputs"][0]["narrative_markdown"] == "hi"
    # garbage
    out = _finalize(spec, "claude-sonnet-4-6", "oops", ctx, 0.0)
    assert out["findings"] == []


def test_run_request_model_config_alias():
    r = RunRequest.model_validate(
        {"subject_type": "individual", "subject": "Jane Doe", "model_config": {"global_default": "claude-opus-4-8"}}
    )
    assert r.model_config_.global_default == "claude-opus-4-8"
