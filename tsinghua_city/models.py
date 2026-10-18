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
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | system
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (Index("ix_transactions_world", "world_id"),)

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
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    read_at: Mapped[float | None] = mapped_column(Float, nullable=True)


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
