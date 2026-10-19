"""Add operator-to-agent message channel."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_operator_messages"
down_revision = "0005_runtime_restart"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_messages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("sender", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("delivered_at", sa.Float(), nullable=True),
        sa.Column("read_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_messages_agent_created",
        "operator_messages",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "ix_operator_messages_world_created",
        "operator_messages",
        ["world_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operator_messages_world_created", table_name="operator_messages")
    op.drop_index("ix_operator_messages_agent_created", table_name="operator_messages")
    op.drop_table("operator_messages")
