"""initial schema (§7)

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="analyst"),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("task", sa.Text(), server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("model_config", postgresql.JSONB(), server_default="{}"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("langfuse_trace_id", sa.String(255), nullable=True),
        sa.Column("cost_usd", sa.Float(), server_default="0"),
        sa.Column("reviewed", sa.Boolean(), server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_subject_type", "runs", ["subject_type"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_created_at", "runs", ["created_at"])
    # §7 composite index
    op.create_index("ix_runs_type_status_created", "runs", ["subject_type", "status", "created_at"])

    op.create_table(
        "workflow_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("is_generated", sa.Boolean(), server_default=sa.false()),
        sa.Column("approved", sa.Boolean(), server_default=sa.false()),
    )
    op.create_index("ix_workflow_plans_run_id", "workflow_plans", ["run_id"])

    op.create_table(
        "run_agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("role", sa.String(255), server_default=""),
        sa.Column("model", sa.String(64), server_default=""),
        sa.Column("provider", sa.String(32), server_default="anthropic"),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("max_iterations", sa.Integer(), server_default="10"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("narrative_markdown", sa.Text(), server_default=""),
        sa.Column("findings", postgresql.JSONB(), server_default="[]"),
        sa.Column("tool_calls", postgresql.JSONB(), server_default="[]"),
    )
    op.create_index("ix_run_agents_run_id", "run_agents", ["run_id"])

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("citation_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default=""),
        sa.Column("publisher", sa.String(255), server_default=""),
        sa.Column("retrieved_at", sa.String(40), nullable=True),
        sa.Column("snippet", sa.Text(), server_default=""),
        sa.Column("content_hash", sa.String(64), server_default=""),
        sa.Column("content", sa.Text(), server_default=""),
        sa.UniqueConstraint("run_id", "citation_id", name="uq_sources_run_citation"),
    )
    op.create_index("ix_sources_run_id", "sources", ["run_id"])

    op.create_table(
        "findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("agent", sa.String(128), server_default=""),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_ids", postgresql.ARRAY(sa.Integer()), server_default="{}"),
        sa.Column("confidence", sa.String(10), server_default="medium"),
        sa.Column("category", sa.String(128), nullable=True),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])
    op.create_index("ix_findings_run_category", "findings", ["run_id", "category"])

    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("report_json", postgresql.JSONB(), nullable=False),
        sa.Column("report_markdown", sa.Text(), server_default=""),
        sa.Column("verification", postgresql.JSONB(), server_default="{}"),
        sa.Column("version", sa.Integer(), server_default="1"),
    )
    op.create_index("ix_reports_run_id", "reports", ["run_id"])

    op.create_table(
        "exports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_exports_run_id", "exports", ["run_id"])

    op.create_table(
        "uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(128), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for t in ("uploads", "exports", "reports", "findings", "sources", "run_agents", "workflow_plans", "runs", "users"):
        op.drop_table(t)
