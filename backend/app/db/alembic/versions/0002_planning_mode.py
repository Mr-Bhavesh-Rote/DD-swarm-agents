"""add planning_mode + max_research_agents to runs

Lets a run choose deterministic template planning vs LLM-tailored planning, and cap the
AI-planned swarm size for cost control (§4.1).

Revision ID: 0002_planning_mode
Revises: 0001_initial
Create Date: 2026-06-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_planning_mode"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("planning_mode", sa.String(20), nullable=False, server_default="template"),
    )
    op.add_column(
        "runs",
        sa.Column("max_research_agents", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "max_research_agents")
    op.drop_column("runs", "planning_mode")
