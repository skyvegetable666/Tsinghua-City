"""API and world-policy schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorldConfig(BaseModel):
    """Initial world parameters. These are experiment conditions, not code."""

    model_config = ConfigDict(extra="forbid")

    initial_credits: int = Field(default=100, ge=0)
    initial_life_seconds: int = Field(default=86_400, gt=0)
    seconds_per_credit: int = Field(default=3_600, gt=0)
    max_message_chars: int = Field(default=4_000, gt=0)


class WorldCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    config: WorldConfig = Field(default_factory=WorldConfig)


class WorldOut(BaseModel):
    id: str
    name: str
    status: str
    created_at: str
    config: WorldConfig


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    initial_credits: int | None = Field(default=None, ge=0)
    initial_life_seconds: int | None = Field(default=None, gt=0)


class AgentOut(BaseModel):
    id: str
    world_id: str
    name: str
    alive: bool
    created_at: str
    death_at: str
    balance: int


class AgentCreated(AgentOut):
    token: str


class BalanceOut(BaseModel):
    balance: int


class StateOut(BaseModel):
    agent: AgentOut
    balance: int


class ExtendLifeRequest(BaseModel):
    credits: int = Field(gt=0)
    client_request_id: str | None = Field(default=None, max_length=128)


class TransferRequest(BaseModel):
    to: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0)
    client_request_id: str | None = Field(default=None, max_length=128)


class MessageCreate(BaseModel):
    to: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1)
    client_message_id: str | None = Field(default=None, max_length=128)


class MessageOut(BaseModel):
    id: str
    world_id: str
    sender: str
    recipient: str
    body: str
    created_at: str
    read_at: str | None


class MarkReadRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)
    reward: int = Field(gt=0)
    deadline_seconds: int | None = Field(default=None, gt=0)


class TaskSubmit(BaseModel):
    submission: str = Field(min_length=1)


class TaskReject(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TaskOut(BaseModel):
    id: str
    world_id: str
    source: str
    publisher: str
    assignee: str | None
    title: str
    description: str
    acceptance_criteria: str
    reward: int
    status: str
    deadline_at: str | None
    submission: str | None
    created_at: str
    claimed_at: str | None
    submitted_at: str | None
    completed_at: str | None
    closed_at: str | None


class EventOut(BaseModel):
    id: str
    event_type: str
    actor: str | None
    created_at: str
    data: dict[str, Any]


class HealthOut(BaseModel):
    ok: bool
    service: str
    version: str
