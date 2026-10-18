"""Add task publication idempotency keys."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_task_idempotency"
down_revision = "0002_public_board"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("client_task_id", sa.String(length=128), nullable=True))
    op.add_column("tasks", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_tasks_idempotency",
        "tasks",
        ["world_id", "publisher_agent_id", "client_task_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_idempotency", table_name="tasks")
    op.drop_column("tasks", "request_hash")
    op.drop_column("tasks", "client_task_id")
