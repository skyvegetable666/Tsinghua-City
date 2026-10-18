"""Database model for authoritative world state.

All consequential records live here. SQLite is the default local backend;
set CITY_DATABASE_URL to a Postgres URL for a deployed experiment.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class World(Base):
    __tablename__ = "worlds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("world_id", "name", name="uq_agents_world_name"),
        Index("ix_agents_world", "world_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    death_at: Mapped[float] = mapped_column(Float, nullable=False)
    alive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("world_id", "agent_id", name="uq_accounts_world_agent"),
        Index("ix_accounts_world", "world_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | system | escrow
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        Index("ix_transactions_world", "world_id"),
        UniqueConstraint(
            "world_id",
            "actor_agent_id",
            "kind",
            "client_request_id",
            name="uq_transactions_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    counterparty_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_entries_transaction", "transaction_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_recipient_created", "recipient_agent_id", "created_at"),
        UniqueConstraint(
            "world_id",
            "sender_agent_id",
            "client_message_id",
            name="uq_messages_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    sender_agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    recipient_agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    client_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    read_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_world_status", "world_id", "status"),
        Index("ix_tasks_assignee", "assignee_agent_id"),
        UniqueConstraint(
            "world_id",
            "publisher_agent_id",
            "client_task_id",
            name="uq_tasks_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    publisher_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    assignee_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | external
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    reward: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    client_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    submission: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    claimed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_world_created", "world_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    author_agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class PostReply(Base):
    __tablename__ = "post_replies"
    __table_args__ = (
        Index("ix_post_replies_post_created", "post_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    author_agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class AgentRuntime(Base):
    __tablename__ = "agent_runtimes"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_runtimes_agent"),
        Index("ix_runtimes_world", "world_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    command: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pid_create_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mcp_config_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    stopped_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_world_created", "world_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
