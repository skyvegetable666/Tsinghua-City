"""Add runtime restart policy and attempt tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_runtime_restart"
down_revision = "0004_agent_runtimes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runtimes",
        sa.Column("restart_policy", sa.String(length=16), nullable=False, server_default="never"),
    )
    op.add_column(
        "agent_runtimes",
        sa.Column("max_restarts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "agent_runtimes",
        sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("agent_runtimes", sa.Column("next_restart_at", sa.Float(), nullable=True))
    op.add_column("agent_runtimes", sa.Column("last_restart_at", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runtimes", "last_restart_at")
    op.drop_column("agent_runtimes", "next_restart_at")
    op.drop_column("agent_runtimes", "restart_count")
    op.drop_column("agent_runtimes", "max_restarts")
    op.drop_column("agent_runtimes", "restart_policy")
