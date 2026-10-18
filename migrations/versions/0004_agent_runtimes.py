"""Add agent runtime process records."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_agent_runtimes"
down_revision = "0003_task_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runtimes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("pid_create_time", sa.Float(), nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.Column("mcp_config_path", sa.String(length=512), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("stopped_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_runtimes_agent"),
    )
    op.create_index("ix_runtimes_world", "agent_runtimes", ["world_id"])


def downgrade() -> None:
    op.drop_table("agent_runtimes")
