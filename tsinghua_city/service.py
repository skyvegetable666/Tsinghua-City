"""Authoritative world operations.

Everything consequential goes through this module: identity, credits, life
deadlines, messages, and events. API routes and MCP tools are thin adapters.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import generate_agent_token, token_hash
from .config import iso, utcnow
from .models import Account, Agent, Event, LedgerEntry, LedgerTransaction, Message, World
from .schemas import WorldConfig

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _world_config(world: World) -> WorldConfig:
    try:
        return WorldConfig.model_validate(world.config or {})
    except Exception as exc:
        raise DomainError(f"invalid world config: {exc}", 500) from exc


def _validate_agent_name(name: str) -> str:
    name = name.strip()
    if not AGENT_NAME_RE.match(name):
        raise DomainError(
            "agent name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
            400,
        )
    if name.lower() == "system":
        raise DomainError("agent name 'system' is reserved", 400)
    return name


def _emit(
    session: Session,
    world_id: str,
    event_type: str,
    *,
    actor_agent_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    row = Event(
        id=_id("ev_"),
        world_id=world_id,
        event_type=event_type,
        actor_agent_id=actor_agent_id,
        created_at=utcnow(),
        data=dict(data or {}),
    )
    session.add(row)
    return row


def _get_world(session: Session, world_id: str) -> World:
    world = session.get(World, world_id)
    if world is None:
        raise DomainError("world not found", 404)
    return world


def _get_agent(session: Session, world_id: str, name: str) -> Agent:
    agent = session.scalars(
        select(Agent).where(Agent.world_id == world_id, Agent.name == name)
    ).one_or_none()
    if agent is None:
        raise DomainError(f"agent not found: {name}", 404)
    return agent


def _system_account(session: Session, world_id: str) -> Account:
    account = session.scalars(
        select(Account).where(Account.world_id == world_id, Account.kind == "system")
    ).one_or_none()
    if account is None:
        raise DomainError("system account missing for world", 500)
    return account


def _agent_account(session: Session, agent: Agent) -> Account:
    account = session.scalars(
        select(Account).where(Account.world_id == agent.world_id, Account.agent_id == agent.id)
    ).one_or_none()
    if account is None:
        raise DomainError("agent account missing", 500)
    return account


def _locked_accounts(session: Session, *account_ids: str) -> dict[str, Account]:
    rows = session.scalars(
        select(Account).where(Account.id.in_(account_ids)).with_for_update()
    ).all()
    result = {row.id: row for row in rows}
    if len(result) != len(set(account_ids)):
        raise DomainError("account not found", 500)
    return result


def create_world(session: Session, name: str, config: WorldConfig) -> World:
    world = World(
        id=_id("w_"),
        name=name.strip(),
        status="active",
        config=config.model_dump(),
        created_at=utcnow(),
    )
    if not world.name:
        raise DomainError("world name must not be empty", 400)
    session.add(world)
    # There are no ORM relationships, so make the FK order explicit: insert the
    # world before its system account and event.
    session.flush()
    session.add(
        Account(
            id=_id("ac_"),
            world_id=world.id,
            agent_id=None,
            kind="system",
            balance=0,
        )
    )
    _emit(session, world.id, "world.created", data={"name": world.name})
    session.commit()
    session.refresh(world)
    return world


def create_agent(
    session: Session,
    world: World,
    name: str,
    *,
    initial_credits: int | None = None,
    initial_life_seconds: int | None = None,
) -> tuple[Agent, str]:
    config = _world_config(world)
    name = _validate_agent_name(name)
    if session.scalars(
        select(Agent).where(Agent.world_id == world.id, Agent.name == name)
    ).one_or_none() is not None:
        raise DomainError(f"agent already exists: {name}", 409)
    credits = config.initial_credits if initial_credits is None else initial_credits
    life = config.initial_life_seconds if initial_life_seconds is None else initial_life_seconds
    if credits < 0:
        raise DomainError("initial_credits must be >= 0", 400)
    if life <= 0:
        raise DomainError("initial_life_seconds must be > 0", 400)

    raw_token = generate_agent_token()
    now = utcnow()
    agent = Agent(
        id=_id("ag_"),
        world_id=world.id,
        name=name,
        token_hash=token_hash(raw_token),
        created_at=now,
        death_at=now + float(life),
        alive=True,
    )
    account = Account(
        id=_id("ac_"),
        world_id=world.id,
        agent_id=agent.id,
        kind="agent",
        balance=0,
    )
    session.add(agent)
    session.flush()
    session.add(account)
    session.flush()

    if credits:
        system = _system_account(session, world.id)
        _post_transfer(
            session,
            world,
            debit=system,
            credit=account,
            amount=credits,
            kind="allocation",
            actor_agent_id=agent.id,
            data={"agent": name, "initial_credits": credits},
        )
    _emit(
        session,
        world.id,
        "agent.created",
        actor_agent_id=agent.id,
        data={"name": name, "death_at": iso(agent.death_at), "credits": credits},
    )
    session.commit()
    session.refresh(agent)
    return agent, raw_token


def agent_from_token(session: Session, raw_token: str) -> Agent | None:
    return session.scalars(
        select(Agent).where(Agent.token_hash == token_hash(raw_token))
    ).one_or_none()


def refresh_alive(session: Session, agent: Agent, world: World) -> bool:
    if not agent.alive:
        return False
    if agent.death_at <= utcnow():
        agent.alive = False
        _emit(
            session,
            world.id,
            "agent.died",
            actor_agent_id=agent.id,
            data={"name": agent.name, "death_at": iso(agent.death_at)},
        )
        session.commit()
        return False
    return True


def require_alive(session: Session, agent: Agent, world: World) -> None:
    if not refresh_alive(session, agent, world):
        raise DomainError("agent is dead; actions are no longer accepted", 409)


def account_balance(session: Session, agent: Agent) -> int:
    return _agent_account(session, agent).balance


def agent_view(session: Session, agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "world_id": agent.world_id,
        "name": agent.name,
        "alive": agent.alive,
        "created_at": iso(agent.created_at),
        "death_at": iso(agent.death_at),
        "balance": account_balance(session, agent),
    }


def world_view(world: World) -> dict[str, Any]:
    return {
        "id": world.id,
        "name": world.name,
        "status": world.status,
        "created_at": iso(world.created_at),
        "config": _world_config(world).model_dump(),
    }


def list_agents(session: Session, world: World, *, exclude_agent_id: str | None = None) -> list[Agent]:
    query = select(Agent).where(Agent.world_id == world.id).order_by(Agent.created_at)
    if exclude_agent_id is not None:
        query = query.where(Agent.id != exclude_agent_id)
    return list(session.scalars(query).all())


def _post_transfer(
    session: Session,
    world: World,
    *,
    debit: Account,
    credit: Account,
    amount: int,
    kind: str,
    actor_agent_id: str | None,
    counterparty_agent_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> LedgerTransaction:
    if amount <= 0:
        raise DomainError("amount must be positive", 400)
    if debit.kind == "agent" and debit.balance < amount:
        raise DomainError("insufficient credits", 409)
    debit.balance -= amount
    credit.balance += amount
    tx = LedgerTransaction(
        id=_id("tx_"),
        world_id=world.id,
        kind=kind,
        actor_agent_id=actor_agent_id,
        counterparty_agent_id=counterparty_agent_id,
        amount=amount,
        data=dict(data or {}),
        created_at=utcnow(),
    )
    session.add(tx)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                transaction_id=tx.id,
                account_id=debit.id,
                delta=-amount,
                balance_after=debit.balance,
            ),
            LedgerEntry(
                transaction_id=tx.id,
                account_id=credit.id,
                delta=amount,
                balance_after=credit.balance,
            ),
        ]
    )
    return tx


def transfer(
    session: Session,
    world: World,
    agent: Agent,
    *,
    to: str,
    amount: int,
) -> LedgerTransaction:
    require_alive(session, agent, world)
    recipient = _get_agent(session, world.id, to.strip())
    if recipient.id == agent.id:
        raise DomainError("cannot transfer to yourself", 400)
    if not refresh_alive(session, recipient, world):
        raise DomainError("recipient is dead", 409)
    if amount <= 0:
        raise DomainError("amount must be positive", 400)
    debit = _agent_account(session, agent)
    credit = _agent_account(session, recipient)
    locked = _locked_accounts(session, debit.id, credit.id)
    debit, credit = locked[debit.id], locked[credit.id]
    tx = _post_transfer(
        session,
        world,
        debit=debit,
        credit=credit,
        amount=amount,
        kind="transfer",
        actor_agent_id=agent.id,
        counterparty_agent_id=recipient.id,
        data={"to": recipient.name, "amount": amount},
    )
    _emit(
        session,
        world.id,
        "credits.transferred",
        actor_agent_id=agent.id,
        data={"to": recipient.name, "amount": amount, "transaction_id": tx.id},
    )
    session.commit()
    return tx


def extend_life(
    session: Session,
    world: World,
    agent: Agent,
    *,
    credits: int,
) -> dict[str, Any]:
    require_alive(session, agent, world)
    if credits <= 0:
        raise DomainError("credits must be positive", 400)
    config = _world_config(world)
    debit = _agent_account(session, agent)
    system = _system_account(session, world.id)
    locked = _locked_accounts(session, debit.id, system.id)
    debit, system = locked[debit.id], locked[system.id]
    seconds = credits * config.seconds_per_credit
    tx = _post_transfer(
        session,
        world,
        debit=debit,
        credit=system,
        amount=credits,
        kind="life_extension",
        actor_agent_id=agent.id,
        data={"credits": credits, "seconds": seconds},
    )
    agent.death_at += float(seconds)
    _emit(
        session,
        world.id,
        "life.extended",
        actor_agent_id=agent.id,
        data={
            "credits": credits,
            "seconds": seconds,
            "death_at": iso(agent.death_at),
            "transaction_id": tx.id,
        },
    )
    session.commit()
    session.refresh(agent)
    return {
        "credits_spent": credits,
        "seconds_added": seconds,
        "death_at": iso(agent.death_at),
        "balance": debit.balance,
    }


def send_message(
    session: Session,
    world: World,
    agent: Agent,
    *,
    to: str,
    body: str,
) -> Message:
    require_alive(session, agent, world)
    recipient = _get_agent(session, world.id, to.strip())
    config = _world_config(world)
    body = body.strip()
    if not body:
        raise DomainError("message body is empty", 400)
    if len(body) > config.max_message_chars:
        raise DomainError(
            f"message too long ({len(body)} chars); limit is {config.max_message_chars}",
            400,
        )
    message = Message(
        id=_id("msg_"),
        world_id=world.id,
        sender_agent_id=agent.id,
        recipient_agent_id=recipient.id,
        body=body,
        created_at=utcnow(),
        read_at=None,
    )
    session.add(message)
    _emit(
        session,
        world.id,
        "message.sent",
        actor_agent_id=agent.id,
        data={"message_id": message.id, "to": recipient.name, "length": len(body)},
    )
    session.commit()
    session.refresh(message)
    return message


def inbox(
    session: Session,
    agent: Agent,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[Message]:
    limit = max(1, min(int(limit), 200))
    query = (
        select(Message)
        .where(Message.recipient_agent_id == agent.id)
        .order_by(Message.created_at)
        .limit(limit)
    )
    if unread_only:
        query = query.where(Message.read_at.is_(None))
    return list(session.scalars(query).all())


def mark_read(session: Session, agent: Agent, message_ids: list[str]) -> int:
    rows = session.scalars(
        select(Message).where(
            Message.recipient_agent_id == agent.id,
            Message.id.in_(message_ids),
        )
    ).all()
    now = utcnow()
    changed = 0
    for message in rows:
        if message.read_at is None:
            message.read_at = now
            changed += 1
    if changed:
        _emit(
            session,
            agent.world_id,
            "message.read",
            actor_agent_id=agent.id,
            data={"count": changed},
        )
        session.commit()
    return changed


def message_view(session: Session, message: Message) -> dict[str, Any]:
    sender = session.get(Agent, message.sender_agent_id)
    recipient = session.get(Agent, message.recipient_agent_id)
    return {
        "id": message.id,
        "world_id": message.world_id,
        "sender": sender.name if sender else message.sender_agent_id,
        "recipient": recipient.name if recipient else message.recipient_agent_id,
        "body": message.body,
        "created_at": iso(message.created_at),
        "read_at": iso(message.read_at),
    }


def reap_expired(session: Session, world: World) -> int:
    now = utcnow()
    agents = session.scalars(
        select(Agent).where(
            Agent.world_id == world.id,
            Agent.alive.is_(True),
            Agent.death_at <= now,
        )
    ).all()
    for agent in agents:
        agent.alive = False
        _emit(
            session,
            world.id,
            "agent.died",
            actor_agent_id=agent.id,
            data={"name": agent.name, "death_at": iso(agent.death_at)},
        )
    if agents:
        session.commit()
    return len(agents)


def events(session: Session, world: World, *, limit: int = 200) -> list[Event]:
    limit = max(1, min(int(limit), 1000))
    return list(
        session.scalars(
            select(Event)
            .where(Event.world_id == world.id)
            .order_by(Event.created_at.desc())
            .limit(limit)
        ).all()
    )


def event_view(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "actor": event.actor_agent_id,
        "created_at": iso(event.created_at),
        "data": event.data,
    }
