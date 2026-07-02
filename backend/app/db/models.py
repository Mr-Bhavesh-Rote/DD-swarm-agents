"""SQLAlchemy 2.0 ORM models (§7).

JSONB is used for report payloads, plans, model_config, findings and tool_calls.
RAW + FINAL report JSON live in reports.report_json — the single source of truth for
the viewer and exporters.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="analyst")  # admin/analyst/viewer
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(512))
    subject_type: Mapped[str] = mapped_column(String(20), index=True)
    task: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued/planning/researching/synthesizing/verifying/done/needs_review/failed/cancelled
    # How the planner builds the swarm: "template" = deterministic YAML (cheap, bounded),
    # "ai" = orchestrator model decomposes the task into a custom swarm (§4.1 path 3).
    planning_mode: Mapped[str] = mapped_column(String(20), default="template")
    # Per-run cap on AI-planned research agents (None = system MAX_SUBAGENTS). Ignored in
    # template mode, where the swarm size is fixed by the YAML.
    max_research_agents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model_config_json: Mapped[dict] = mapped_column("model_config", JSONB, default=dict)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    langfuse_trace_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)  # human-approval gate (§10)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan = relationship("WorkflowPlanRow", back_populates="run", uselist=False, cascade="all, delete-orphan")
    agents = relationship("RunAgent", back_populates="run", cascade="all, delete-orphan")
    sources = relationship("SourceRow", back_populates="run", cascade="all, delete-orphan")
    findings = relationship("FindingRow", back_populates="run", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="run", cascade="all, delete-orphan")


class WorkflowPlanRow(Base):
    __tablename__ = "workflow_plans"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    plan: Mapped[dict] = mapped_column(JSONB)
    is_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)  # plan-approval gate (§10)
    run = relationship("Run", back_populates="plan")


class RunAgent(Base):
    __tablename__ = "run_agents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(32), default="anthropic")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    max_iterations: Mapped[int] = mapped_column(Integer, default=10)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    narrative_markdown: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[list] = mapped_column(JSONB, default=list)
    tool_calls: Mapped[list] = mapped_column(JSONB, default=list)
    run = relationship("Run", back_populates="agents")


class SourceRow(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("run_id", "citation_id", name="uq_sources_run_citation"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    citation_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    publisher: Mapped[str] = mapped_column(String(255), default="")
    retrieved_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    run = relationship("Run", back_populates="sources")


class FindingRow(Base):
    __tablename__ = "findings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(128), default="")
    claim: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list] = mapped_column(ARRAY(Integer), default=list)
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    run = relationship("Run", back_populates="findings")


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # raw / final
    report_json: Mapped[dict] = mapped_column(JSONB)
    report_markdown: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    run = relationship("Run", back_populates="reports")


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # raw / final
    format: Mapped[str] = mapped_column(String(10))  # pdf / docx
    storage_uri: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
