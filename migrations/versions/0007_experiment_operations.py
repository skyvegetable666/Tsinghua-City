"""Experiment registry, employer funding, conversations, and runtime ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0007_experiment_operations"
down_revision = "0006_operator_messages"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_grants",
        sa.Column("runtime_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("owner_host", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("runtime_id"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("owner_host", sa.String(length=128), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("measurements", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("experiment_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_experiment_runs_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_experiment_runs_status"), ["status"], unique=False)

    op.create_table(
        "conversation_members",
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.PrimaryKeyConstraint("conversation_id", "agent_id"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("sender_agent_id", sa.String(length=32), nullable=False),
        sa.Column("client_message_id", sa.String(length=128), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sender_agent_id"],
            ["agents.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sender_agent_id", "client_message_id"),
    )
    op.create_table(
        "employers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("world_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id"),
        sa.UniqueConstraint("world_id", "name"),
    )
    with op.batch_alter_table("agent_runtimes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_host", sa.String(length=128), nullable=True))

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("employer_id", sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_employer", "employers", ["employer_id"], ["id"], ondelete="RESTRICT"
        )


def downgrade():
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_constraint("fk_tasks_employer", type_="foreignkey")
        batch_op.drop_column("employer_id")

    with op.batch_alter_table("agent_runtimes", schema=None) as batch_op:
        batch_op.drop_column("owner_host")

    op.drop_table("employers")
    op.drop_table("conversation_messages")
    op.drop_table("conversation_members")
    with op.batch_alter_table("experiment_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_experiment_runs_status"))
        batch_op.drop_index(batch_op.f("ix_experiment_runs_batch_id"))

    op.drop_table("experiment_runs")
    op.drop_table("conversations")
    op.drop_table("model_grants")
