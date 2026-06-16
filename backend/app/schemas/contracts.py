"""Binding JSON data contracts (§5).

These Pydantic v2 models are the wire + storage representation for the whole platform.
The workflow engine, the API and the exporters all speak these shapes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

SubjectType = Literal["company", "individual"]
Confidence = Literal["high", "medium", "low"]


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

    model_config = {"populate_by_name": True}  # type: ignore[assignment]


# --- 5.2 WorkflowPlan ---
class AgentSpec(BaseModel):
    name: str
    role: str
    goal: str
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


class FinalReport(BaseModel):
    run_id: str
    subject: str
    subject_type: SubjectType
    generated_at: str
    model_summary: Dict[str, str] = Field(default_factory=dict)
    verification: Verification = Field(default_factory=Verification)
    sections: List[ReportSection] = Field(default_factory=list)
    sources: List[Source] = Field(default_factory=list)


RunRequest.model_rebuild()
