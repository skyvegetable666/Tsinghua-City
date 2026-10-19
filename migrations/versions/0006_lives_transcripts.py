"""Add agent lives and transcript events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_lives_transcripts"
down_revision = "0005_runtime_restart"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_lives",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("ended_at", sa.Float(), nullable=True),
        sa.Column("end_reason", sa.String(length=128), nullable=True),
        sa.Column("wake", sa.Text(), nullable=True),
        sa.Column("diary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_lives_world_agent_started",
        "agent_lives",
        ["world_id", "agent_id", "started_at"],
    )
    op.create_index("ix_agent_lives_agent_status", "agent_lives", ["agent_id", "status"])

    op.create_table(
        "agent_transcript_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("life_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["life_id"], ["agent_lives.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transcript_life_created",
        "agent_transcript_events",
        ["life_id", "created_at"],
    )
    op.create_index(
        "ix_transcript_agent_created",
        "agent_transcript_events",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_transcript_agent_created", table_name="agent_transcript_events")
    op.drop_index("ix_transcript_life_created", table_name="agent_transcript_events")
    op.drop_table("agent_transcript_events")
    op.drop_index("ix_agent_lives_agent_status", table_name="agent_lives")
    op.drop_index("ix_agent_lives_world_agent_started", table_name="agent_lives")
    op.drop_table("agent_lives")
