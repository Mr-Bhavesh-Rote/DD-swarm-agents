"""Binding JSON data contracts (§5).

These Pydantic v2 models are the wire + storage representation for the whole platform.
The workflow engine, the API and the exporters all speak these shapes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SubjectType = Literal["company", "individual"]
Confidence = Literal["high", "medium", "low"]
PlanningMode = Literal["template", "ai"]
AgentDomain = Literal[
    "overview_ownership",
    "sanctions_legal",
    "adverse_conduct",
    "adverse_media_esg",
    "pep_ownership_risk",
]


# --- 5.1 RunRequest (UI -> API) ---
class ModelConfig(BaseModel):
    global_default: Optional[str] = None
    role_overrides: Dict[str, str] = Field(default_factory=dict)


class RunRequest(BaseModel):
    subject_type: SubjectType
    subject: str
    task: str = ""
    model_config_: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    plan_override: Optional["WorkflowPlan"] = None
    uploaded_file_ids: List[str] = Field(default_factory=list)
    # "template" = deterministic YAML swarm (cheap); "ai" = orchestrator builds a custom
    # swarm from the task. max_research_agents caps the AI swarm (None = system default).
    planning_mode: PlanningMode = "template"
    max_research_agents: Optional[int] = Field(default=5, ge=1, le=16)

    @field_validator("planning_mode", mode="before")
    @classmethod
    def _coerce_planning_mode(cls, v):
        return v if v else "template"

    model_config = {"populate_by_name": True}  # type: ignore[assignment]


# --- 5.1b Task refine (plain English -> structured task prompt) ---
class TaskRefineRequest(BaseModel):
    subject_type: SubjectType
    subject: str = ""
    query: str  # the analyst's plain-English ask


class TaskRefineResponse(BaseModel):
    task: str
    cost_usd: float = 0.0


# --- 5.2 WorkflowPlan ---
class AgentSpec(BaseModel):
    name: str
    role: str
    goal: str
    domain: AgentDomain = "overview_ownership"
    rationale: str = ""
    depends_on: List[str] = Field(default_factory=list)
    max_iterations: int = 10
    suggested_tools: List[str] = Field(default_factory=lambda: ["web_search", "scraper"])
    model: Optional[str] = None
    provider: str = "anthropic"
    credential_id: Optional[str] = None


class WorkflowPlan(BaseModel):
    task: str = ""
    summary: str = ""
    execution_notes: str = ""
    agents: List[AgentSpec] = Field(default_factory=list)

    def research_agents(self) -> List[AgentSpec]:
        """Agents that perform research (have search/scrape tools) — the swarm."""
        return [
            a
            for a in self.agents
            if any(t in a.suggested_tools for t in ("web_search", "scraper"))
        ]

    def aggregator_agents(self) -> List[AgentSpec]:
        return [
            a
            for a in self.agents
            if a not in self.research_agents()
        ]


# --- 5.3 Source / Citation ---
class Source(BaseModel):
    id: int
    url: str
    title: str = ""
    publisher: str = ""
    retrieved_at: Optional[str] = None
    snippet: str = ""
    content_hash: str = ""
    content: str = ""  # full fetched text — needed by the verifier; stripped from wire if large


# --- 5.4 Finding ---
class Finding(BaseModel):
    agent: str
    claim: str
    source_ids: List[int] = Field(default_factory=list)
    confidence: Confidence = "medium"
    category: Optional[str] = None


# --- 5.5 RAW report ---
class ToolCall(BaseModel):
    tool: str
    input: Dict[str, Any] = Field(default_factory=dict)
    output_summary: str = ""


class AgentOutput(BaseModel):
    agent: str
    role: str = ""
    model: str = ""
    narrative_markdown: str = ""
    findings: List[Finding] = Field(default_factory=list)
    tool_calls: List[ToolCall] = Field(default_factory=list)


class RawReport(BaseModel):
    run_id: str
    subject: str
    subject_type: SubjectType
    generated_at: str
    agent_outputs: List[AgentOutput] = Field(default_factory=list)
    sources: List[Source] = Field(default_factory=list)


# --- 5.6 FINAL report ---
class SectionTable(BaseModel):
    title: str = ""
    columns: List[str] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)


class ReportSection(BaseModel):
    id: str
    title: str
    body_markdown: str = ""
    tables: List[SectionTable] = Field(default_factory=list)
    citations: List[int] = Field(default_factory=list)


class VerificationFlag(BaseModel):
    section_id: str
    claim: str
    citation_ids: List[int] = Field(default_factory=list)
    reason: str
    status: Literal["unsupported", "unverified", "estimate"] = "unsupported"


class Verification(BaseModel):
    citation_coverage: float = 0.0
    faithfulness_score: float = 0.0
    flags: List[VerificationFlag] = Field(default_factory=list)


class QualityGateResult(BaseModel):
    gate: str
    status: Literal["pass", "fail"]
    threshold: float = 0.0
    actual: float = 0.0
    message: str = ""


class QualityAssessment(BaseModel):
    status: Literal["pass", "pass_with_caveats", "fail"] = "fail"
    quality_score: int = 0
    gates_passed: int = 0
    gates_total: int = 4
    gates: List[QualityGateResult] = Field(default_factory=list)
    finding_segmentation: Dict[str, int] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)


class FinalReport(BaseModel):
    run_id: str
    subject: str
    subject_type: SubjectType
    generated_at: str
    model_summary: Dict[str, str] = Field(default_factory=dict)
    verification: Verification = Field(default_factory=Verification)
    quality_assessment: Dict[str, Any] = Field(default_factory=dict)
    sections: List[ReportSection] = Field(default_factory=list)
    sources: List[Source] = Field(default_factory=list)


RunRequest.model_rebuild()
