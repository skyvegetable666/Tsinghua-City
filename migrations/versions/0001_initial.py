"""Initial world schema (explicit snapshot).

This revision is an explicit snapshot so later model changes do not silently
alter the baseline. Subsequent schema changes belong in new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worlds",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("death_at", sa.Float(), nullable=False),
        sa.Column("alive", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("world_id", "name", name="uq_agents_world_name"),
    )
    op.create_index("ix_agents_world", "agents", ["world_id"])
    op.create_index("ix_agents_token_hash", "agents", ["token_hash"], unique=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("world_id", "agent_id", name="uq_accounts_world_agent"),
    )
    op.create_index("ix_accounts_world", "accounts", ["world_id"])

    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor_agent_id", sa.String(length=32), nullable=True),
        sa.Column("counterparty_agent_id", sa.String(length=32), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["counterparty_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "world_id",
            "actor_agent_id",
            "kind",
            "client_request_id",
            name="uq_transactions_idempotency",
        ),
    )
    op.create_index("ix_transactions_world", "ledger_transactions", ["world_id"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transaction_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["ledger_transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entries_transaction", "ledger_entries", ["transaction_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("sender_agent_id", sa.String(length=32), nullable=False),
        sa.Column("recipient_agent_id", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("read_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["recipient_agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "world_id",
            "sender_agent_id",
            "client_message_id",
            name="uq_messages_idempotency",
        ),
    )
    op.create_index(
        "ix_messages_recipient_created", "messages", ["recipient_agent_id", "created_at"]
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("publisher_agent_id", sa.String(length=32), nullable=True),
        sa.Column("assignee_agent_id", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("reward", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deadline_at", sa.Float(), nullable=True),
        sa.Column("submission", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("claimed_at", sa.Float(), nullable=True),
        sa.Column("submitted_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("closed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["assignee_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["publisher_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_world_status", "tasks", ["world_id", "status"])
    op.create_index("ix_tasks_assignee", "tasks", ["assignee_agent_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_agent_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_world_created", "events", ["world_id", "created_at"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("tasks")
    op.drop_table("messages")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("accounts")
    op.drop_table("agents")
    op.drop_table("worlds")
