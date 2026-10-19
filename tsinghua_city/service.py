"""Authoritative world operations.

Everything consequential goes through this module: identity, credits, life
deadlines, messages, and events. API routes and MCP tools are thin adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import generate_agent_token, token_hash
from .config import iso, utcnow
from .models import (
    Account,
    Agent,
    Employer,
    Event,
    LedgerEntry,
    LedgerTransaction,
    Message,
    OperatorMessage,
    Post,
    PostReply,
    Task,
    World,
)
from .schemas import WorldConfig

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _request_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 128:
        raise DomainError("client_request_id must be at most 128 characters", 400)
    return value


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_transaction(
    session: Session,
    *,
    world_id: str,
    actor_agent_id: str,
    kind: str,
    client_request_id: str,
) -> LedgerTransaction | None:
    return session.scalars(
        select(LedgerTransaction).where(
            LedgerTransaction.world_id == world_id,
            LedgerTransaction.actor_agent_id == actor_agent_id,
            LedgerTransaction.kind == kind,
            LedgerTransaction.client_request_id == client_request_id,
        )
    ).one_or_none()


def _find_message(
    session: Session,
    *,
    world_id: str,
    sender_agent_id: str,
    client_message_id: str,
) -> Message | None:
    return session.scalars(
        select(Message).where(
            Message.world_id == world_id,
            Message.sender_agent_id == sender_agent_id,
            Message.client_message_id == client_message_id,
        )
    ).one_or_none()


def _same_request(existing_hash: str | None, expected_hash: str) -> bool:
    return bool(existing_hash) and existing_hash == expected_hash


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


def _escrow_account(session: Session, world_id: str) -> Account:
    account = session.scalars(
        select(Account).where(Account.world_id == world_id, Account.kind == "escrow")
    ).one_or_none()
    if account is None:
        raise DomainError("escrow account missing for world", 500)
    return account


def _agent_account(session: Session, agent: Agent) -> Account:
    account = session.scalars(
        select(Account).where(Account.world_id == agent.world_id, Account.agent_id == agent.id)
    ).one_or_none()
    if account is None:
        raise DomainError("agent account missing", 500)
    return account


def _locked_accounts(session: Session, *account_ids: str) -> dict[str, Account]:
    # populate_existing is essential: an earlier unlocked read (for example via
    # _agent_account) leaves the Account in the identity map. SELECT FOR UPDATE
    # blocks correctly, but without populate_existing SQLAlchemy would keep the
    # stale balance after the lock is acquired, defeating the balance check.
    rows = session.scalars(
        select(Account)
        .where(Account.id.in_(account_ids))
        .order_by(Account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
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
    session.add_all(
        [
            Account(
                id=_id("ac_"),
                world_id=world.id,
                agent_id=None,
                kind="system",
                balance=0,
            ),
            Account(
                id=_id("ac_"),
                world_id=world.id,
                agent_id=None,
                kind="escrow",
                balance=0,
            ),
        ]
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


def rotate_agent_token(session: Session, agent: Agent) -> str:
    """Issue a fresh agent token; caller owns the transaction/commit."""
    raw = generate_agent_token()
    agent.token_hash = token_hash(raw)
    session.flush()
    return raw


def record_event(
    session: Session,
    world_id: str,
    event_type: str,
    *,
    actor_agent_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    return _emit(
        session,
        world_id,
        event_type,
        actor_agent_id=actor_agent_id,
        data=data,
    )


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
    client_request_id: str | None = None,
    request_hash: str | None = None,
) -> LedgerTransaction:
    if amount <= 0:
        raise DomainError("amount must be positive", 400)
    if debit.kind in {"agent", "employer"} and debit.balance < amount:
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
        client_request_id=client_request_id,
        request_hash=request_hash,
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


def _extension_result(session: Session, agent: Agent, tx: LedgerTransaction) -> dict[str, Any]:
    data = tx.data or {}
    return {
        "credits_spent": int(data.get("credits") or tx.amount),
        "seconds_added": int(data.get("seconds") or 0),
        "death_at": iso(agent.death_at),
        "balance": account_balance(session, agent),
    }


def transfer(
    session: Session,
    world: World,
    agent: Agent,
    *,
    to: str,
    amount: int,
    client_request_id: str | None = None,
) -> LedgerTransaction:
    key = _request_id(client_request_id)
    recipient = _get_agent(session, world.id, to.strip())
    if recipient.id == agent.id:
        raise DomainError("cannot transfer to yourself", 400)
    if amount <= 0:
        raise DomainError("amount must be positive", 400)
    request_payload = {"to": recipient.name, "amount": amount}
    request_hash = _request_hash(request_payload)
    if key is not None:
        existing = _find_transaction(
            session,
            world_id=world.id,
            actor_agent_id=agent.id,
            kind="transfer",
            client_request_id=key,
        )
        if existing is not None:
            if not _same_request(existing.request_hash, request_hash):
                raise DomainError("client_request_id reused with a different transfer", 409)
            return existing
    require_alive(session, agent, world)
    if not refresh_alive(session, recipient, world):
        raise DomainError("recipient is dead", 409)
    try:
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
            data=request_payload,
            client_request_id=key,
            request_hash=request_hash,
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
    except IntegrityError as exc:
        session.rollback()
        if key is not None:
            existing = _find_transaction(
                session,
                world_id=world.id,
                actor_agent_id=agent.id,
                kind="transfer",
                client_request_id=key,
            )
            if existing is not None and _same_request(existing.request_hash, request_hash):
                return existing
        raise DomainError("concurrent transfer conflict", 409) from exc


def extend_life(
    session: Session,
    world: World,
    agent: Agent,
    *,
    credits: int,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    if credits <= 0:
        raise DomainError("credits must be positive", 400)
    config = _world_config(world)
    seconds = credits * config.seconds_per_credit
    key = _request_id(client_request_id)
    request_hash = _request_hash({"credits": credits})
    if key is not None:
        existing = _find_transaction(
            session,
            world_id=world.id,
            actor_agent_id=agent.id,
            kind="life_extension",
            client_request_id=key,
        )
        if existing is not None:
            if not _same_request(existing.request_hash, request_hash):
                raise DomainError("client_request_id reused with a different extension", 409)
            return _extension_result(session, agent, existing)
    require_alive(session, agent, world)
    try:
        debit = _agent_account(session, agent)
        system = _system_account(session, world.id)
        locked = _locked_accounts(session, debit.id, system.id)
        debit, system = locked[debit.id], locked[system.id]
        tx = _post_transfer(
            session,
            world,
            debit=debit,
            credit=system,
            amount=credits,
            kind="life_extension",
            actor_agent_id=agent.id,
            data={"credits": credits, "seconds": seconds},
            client_request_id=key,
            request_hash=request_hash,
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
    except IntegrityError as exc:
        session.rollback()
        if key is not None:
            existing = _find_transaction(
                session,
                world_id=world.id,
                actor_agent_id=agent.id,
                kind="life_extension",
                client_request_id=key,
            )
            if existing is not None and _same_request(existing.request_hash, request_hash):
                return _extension_result(session, agent, existing)
        raise DomainError("concurrent extension conflict", 409) from exc


def send_message(
    session: Session,
    world: World,
    agent: Agent,
    *,
    to: str,
    body: str,
    client_message_id: str | None = None,
) -> Message:
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
    key = _request_id(client_message_id)
    request_hash = _request_hash({"to": recipient.name, "body": body})
    if key is not None:
        existing = _find_message(
            session,
            world_id=world.id,
            sender_agent_id=agent.id,
            client_message_id=key,
        )
        if existing is not None:
            if not _same_request(existing.request_hash, request_hash):
                raise DomainError("client_message_id reused with a different message", 409)
            return existing
    require_alive(session, agent, world)
    try:
        message = Message(
            id=_id("msg_"),
            world_id=world.id,
            sender_agent_id=agent.id,
            recipient_agent_id=recipient.id,
            body=body,
            client_message_id=key,
            request_hash=request_hash,
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
    except IntegrityError as exc:
        session.rollback()
        if key is not None:
            existing = _find_message(
                session,
                world_id=world.id,
                sender_agent_id=agent.id,
                client_message_id=key,
            )
            if existing is not None and _same_request(existing.request_hash, request_hash):
                return existing
        raise DomainError("concurrent message conflict", 409) from exc


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


# ------------------------------------------------------------ operator channel


def _validate_operator_body(world: World, body: str) -> str:
    body = body.strip()
    if not body:
        raise DomainError("operator message body must not be empty", 400)
    limit = min(8_000, _world_config(world).max_message_chars)
    if len(body) > limit:
        raise DomainError(f"operator message too long ({len(body)} chars); limit is {limit}", 400)
    return body


def send_operator_message(
    session: Session,
    world: World,
    agent: Agent,
    body: str,
) -> OperatorMessage:
    message = OperatorMessage(
        id=_id("op_"),
        world_id=world.id,
        agent_id=agent.id,
        sender="operator",
        body=_validate_operator_body(world, body),
        created_at=utcnow(),
    )
    session.add(message)
    _emit(
        session,
        world.id,
        "operator.message.sent",
        actor_agent_id=agent.id,
        data={"message_id": message.id, "direction": "operator_to_agent"},
    )
    session.commit()
    return message


def send_agent_operator_message(
    session: Session,
    world: World,
    agent: Agent,
    body: str,
) -> OperatorMessage:
    message = OperatorMessage(
        id=_id("op_"),
        world_id=world.id,
        agent_id=agent.id,
        sender="agent",
        body=_validate_operator_body(world, body),
        created_at=utcnow(),
    )
    session.add(message)
    _emit(
        session,
        world.id,
        "operator.message.sent",
        actor_agent_id=agent.id,
        data={"message_id": message.id, "direction": "agent_to_operator"},
    )
    session.commit()
    return message


def list_operator_messages(
    session: Session,
    agent: Agent,
    *,
    limit: int = 100,
    from_operator: bool | None = None,
) -> list[OperatorMessage]:
    limit = max(1, min(int(limit), 500))
    query = select(OperatorMessage).where(OperatorMessage.agent_id == agent.id)
    if from_operator is True:
        query = query.where(OperatorMessage.sender == "operator")
    elif from_operator is False:
        query = query.where(OperatorMessage.sender == "agent")
    return list(
        session.scalars(query.order_by(OperatorMessage.created_at).limit(limit)).all()
    )


def mark_operator_messages_read(
    session: Session,
    agent: Agent,
    message_ids: list[str],
) -> int:
    now = utcnow()
    changed = 0
    rows = session.scalars(
        select(OperatorMessage).where(
            OperatorMessage.agent_id == agent.id,
            OperatorMessage.id.in_(message_ids),
            OperatorMessage.sender == "operator",
        )
    ).all()
    for message in rows:
        if message.delivered_at is None:
            message.delivered_at = now
        if message.read_at is None:
            message.read_at = now
            changed += 1
    if changed:
        _emit(
            session,
            agent.world_id,
            "operator.message.read",
            actor_agent_id=agent.id,
            data={"count": changed},
        )
        session.commit()
    return changed


def operator_message_view(message: OperatorMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "world_id": message.world_id,
        "sender": message.sender,
        "body": message.body,
        "created_at": iso(message.created_at),
        "delivered_at": iso(message.delivered_at),
        "read_at": iso(message.read_at),
    }


# ------------------------------------------------------------ public board

POST_TITLE_MAX = 200
POST_BODY_MAX = 8_000


def _validate_post_fields(title: str, body: str) -> tuple[str, str]:
    title = title.strip()
    body = body.strip()
    if not title or len(title) > POST_TITLE_MAX:
        raise DomainError("post title must be 1-200 characters", 400)
    if not body:
        raise DomainError("post body must not be empty", 400)
    if len(body) > POST_BODY_MAX:
        raise DomainError(f"post body too long; limit is {POST_BODY_MAX}", 400)
    return title, body


def _reply_view(session: Session, reply: PostReply) -> dict[str, Any]:
    author = session.get(Agent, reply.author_agent_id)
    return {
        "id": reply.id,
        "post_id": reply.post_id,
        "author": author.name if author else reply.author_agent_id,
        "body": reply.body,
        "created_at": iso(reply.created_at),
    }


def _post_view(
    session: Session,
    post: Post,
    *,
    include_replies: bool,
) -> dict[str, Any]:
    author = session.get(Agent, post.author_agent_id)
    data: dict[str, Any] = {
        "id": post.id,
        "world_id": post.world_id,
        "author": author.name if author else post.author_agent_id,
        "title": post.title,
        "body": post.body,
        "created_at": iso(post.created_at),
    }
    if include_replies:
        replies = session.scalars(
            select(PostReply)
            .where(PostReply.post_id == post.id)
            .order_by(PostReply.created_at)
        ).all()
        data["replies"] = [_reply_view(session, reply) for reply in replies]
    return data


def publish_post(session: Session, world: World, agent: Agent, *, title: str, body: str) -> Post:
    require_alive(session, agent, world)
    title, body = _validate_post_fields(title, body)
    post = Post(
        id=_id("post_"),
        world_id=world.id,
        author_agent_id=agent.id,
        title=title,
        body=body,
        created_at=utcnow(),
    )
    session.add(post)
    _emit(
        session,
        world.id,
        "post.published",
        actor_agent_id=agent.id,
        data={"post_id": post.id, "title": title},
    )
    session.commit()
    session.refresh(post)
    return post


def list_posts(session: Session, world: World, *, limit: int = 100) -> list[Post]:
    limit = max(1, min(int(limit), 200))
    return list(
        session.scalars(
            select(Post)
            .where(Post.world_id == world.id)
            .order_by(Post.created_at.desc())
            .limit(limit)
        ).all()
    )


def get_post(session: Session, world: World, post_id: str) -> Post:
    post = session.scalars(
        select(Post).where(Post.id == post_id, Post.world_id == world.id)
    ).one_or_none()
    if post is None:
        raise DomainError("post not found", 404)
    return post


def post_view(
    session: Session,
    post: Post,
    *,
    include_replies: bool = False,
) -> dict[str, Any]:
    return _post_view(session, post, include_replies=include_replies)


def reply_to_post(
    session: Session,
    world: World,
    agent: Agent,
    post_id: str,
    body: str,
) -> PostReply:
    require_alive(session, agent, world)
    post = get_post(session, world, post_id)
    body = body.strip()
    if not body:
        raise DomainError("reply body must not be empty", 400)
    if len(body) > POST_BODY_MAX:
        raise DomainError(f"reply too long; limit is {POST_BODY_MAX}", 400)
    reply = PostReply(
        id=_id("reply_"),
        world_id=world.id,
        post_id=post.id,
        author_agent_id=agent.id,
        body=body,
        created_at=utcnow(),
    )
    session.add(reply)
    _emit(
        session,
        world.id,
        "post.replied",
        actor_agent_id=agent.id,
        data={"post_id": post.id, "reply_id": reply.id},
    )
    session.commit()
    session.refresh(reply)
    return reply


def reply_view(session: Session, reply: PostReply) -> dict[str, Any]:
    return _reply_view(session, reply)


# ---------------------------------------------------------------- task board

TASK_OPEN = "OPEN"
TASK_CLAIMED = "CLAIMED"
TASK_SUBMITTED = "SUBMITTED"
TASK_COMPLETED = "COMPLETED"
TASK_CANCELLED = "CANCELLED"
TASK_FAILED = "FAILED"


def _get_task(
    session: Session,
    world: World,
    task_id: str,
    *,
    lock: bool = False,
) -> Task:
    query = select(Task).where(Task.id == task_id, Task.world_id == world.id)
    if lock:
        query = query.with_for_update()
    task = session.scalars(query).one_or_none()
    if task is None:
        raise DomainError("task not found", 404)
    return task


def _validate_task_fields(
    title: str,
    description: str,
    acceptance_criteria: str,
    reward: int,
    deadline_seconds: int | None,
) -> tuple[str, str, str, int | None]:
    title = title.strip()
    description = description.strip()
    acceptance_criteria = acceptance_criteria.strip()
    if not title or len(title) > 200:
        raise DomainError("task title must be 1-200 characters", 400)
    if not description:
        raise DomainError("task description must not be empty", 400)
    if not acceptance_criteria:
        raise DomainError("task acceptance_criteria must not be empty", 400)
    if reward <= 0:
        raise DomainError("task reward must be positive", 400)
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise DomainError("deadline_seconds must be positive", 400)
    return title, description, acceptance_criteria, deadline_seconds


def _authorize_task_manager(
    session: Session,
    world: World,
    task: Task,
    actor: Agent | None,
    *,
    is_admin: bool,
) -> None:
    if is_admin:
        return
    if task.publisher_agent_id is None:
        raise DomainError("external task can only be managed by an admin", 403)
    if actor is None or actor.id != task.publisher_agent_id:
        raise DomainError("only the publisher can manage this task", 403)
    require_alive(session, actor, world)


def _task_funder_account(session: Session, task: Task) -> Account:
    if task.employer_id:
        employer = session.get(Employer, task.employer_id)
        return session.get(Account, employer.account_id)
    if task.publisher_agent_id is None:
        return _system_account(session, task.world_id)
    publisher = session.get(Agent, task.publisher_agent_id)
    if publisher is None:
        raise DomainError("task publisher no longer exists", 500)
    return _agent_account(session, publisher)


def _task_view(session: Session, task: Task) -> dict[str, Any]:
    publisher = session.get(Agent, task.publisher_agent_id) if task.publisher_agent_id else None
    employer = session.get(Employer, task.employer_id) if task.employer_id else None
    assignee = session.get(Agent, task.assignee_agent_id) if task.assignee_agent_id else None
    return {
        "id": task.id,
        "world_id": task.world_id,
        "source": task.source,
        "publisher": publisher.name if publisher else employer.name if employer else "external",
        "employer_id": task.employer_id,
        "assignee": assignee.name if assignee else None,
        "title": task.title,
        "description": task.description,
        "acceptance_criteria": task.acceptance_criteria,
        "reward": task.reward,
        "status": task.status,
        "deadline_at": iso(task.deadline_at),
        "submission": task.submission,
        "created_at": iso(task.created_at),
        "claimed_at": iso(task.claimed_at),
        "submitted_at": iso(task.submitted_at),
        "completed_at": iso(task.completed_at),
        "closed_at": iso(task.closed_at),
    }


def _find_task_by_key(
    session: Session,
    world_id: str,
    publisher_agent_id: str | None,
    client_task_id: str,
    employer_id: str | None = None,
) -> Task | None:
    query = select(Task).where(
        Task.world_id == world_id,
        Task.client_task_id == client_task_id,
        Task.employer_id == employer_id,
    )
    if publisher_agent_id is None:
        query = query.where(Task.publisher_agent_id.is_(None))
    else:
        query = query.where(Task.publisher_agent_id == publisher_agent_id)
    return session.scalars(query).one_or_none()


def _create_task(
    session: Session,
    world: World,
    *,
    publisher: Agent | None,
    source: str,
    title: str,
    description: str,
    acceptance_criteria: str,
    reward: int,
    deadline_seconds: int | None,
    client_task_id: str | None = None,
    employer: Employer | None = None,
) -> Task:
    title, description, acceptance_criteria, deadline_seconds = _validate_task_fields(
        title, description, acceptance_criteria, reward, deadline_seconds
    )
    key = _request_id(client_task_id)
    request_hash = _request_hash(
        {
            "source": source,
            **({"employer_id": employer.id} if employer else {}),
            "title": title,
            "description": description,
            "acceptance_criteria": acceptance_criteria,
            "reward": reward,
            "deadline_seconds": deadline_seconds,
        }
    )
    publisher_id = publisher.id if publisher else None
    if key is not None:
        existing = _find_task_by_key(session, world.id, publisher_id, key,
                                     employer.id if employer else None)
        if existing is not None:
            if not _same_request(existing.request_hash, request_hash):
                raise DomainError("client_task_id reused with different task fields", 409)
            return existing

    now = utcnow()
    try:
        task = Task(
            id=_id("task_"),
            world_id=world.id,
            publisher_agent_id=publisher_id,
            employer_id=employer.id if employer else None,
            assignee_agent_id=None,
            source=source,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            reward=reward,
            status=TASK_OPEN,
            client_task_id=key,
            request_hash=request_hash,
            deadline_at=now + float(deadline_seconds) if deadline_seconds else None,
            submission=None,
            created_at=now,
        )
        session.add(task)
        session.flush()

        if publisher is None:
            debit = session.get(Account, employer.account_id) if employer else _system_account(
                session, world.id
            )
            kind = "external_task_escrow"
            actor_id = None
        else:
            debit = _agent_account(session, publisher)
            kind = "task_escrow"
            actor_id = publisher.id
        escrow = _escrow_account(session, world.id)
        locked = _locked_accounts(session, debit.id, escrow.id)
        debit, escrow = locked[debit.id], locked[escrow.id]
        tx = _post_transfer(
            session,
            world,
            debit=debit,
            credit=escrow,
            amount=reward,
            kind=kind,
            actor_agent_id=actor_id,
            data={"task_id": task.id, "title": title, "reward": reward},
        )
        _emit(
            session,
            world.id,
            "task.published",
            actor_agent_id=actor_id,
            data={
                "task_id": task.id,
                "title": title,
                "reward": reward,
                "source": source,
                "transaction_id": tx.id,
            },
        )
        session.commit()
        session.refresh(task)
        return task
    except IntegrityError as exc:
        session.rollback()
        if key is not None:
            existing = _find_task_by_key(session, world.id, publisher_id, key,
                                         employer.id if employer else None)
            if existing is not None and _same_request(existing.request_hash, request_hash):
                return existing
        raise DomainError("concurrent task publication conflict", 409) from exc


def publish_task(
    session: Session,
    world: World,
    agent: Agent,
    *,
    title: str,
    description: str,
    acceptance_criteria: str,
    reward: int,
    deadline_seconds: int | None = None,
    client_task_id: str | None = None,
) -> Task:
    require_alive(session, agent, world)
    return _create_task(
        session,
        world,
        publisher=agent,
        source="agent",
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        reward=reward,
        deadline_seconds=deadline_seconds,
        client_task_id=client_task_id,
    )


def publish_external_task(
    session: Session,
    world: World,
    *,
    title: str,
    description: str,
    acceptance_criteria: str,
    reward: int,
    deadline_seconds: int | None = None,
    client_task_id: str | None = None,
) -> Task:
    return _create_task(
        session,
        world,
        publisher=None,
        source="external",
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        reward=reward,
        deadline_seconds=deadline_seconds,
        client_task_id=client_task_id,
    )


def task_view(session: Session, task: Task) -> dict[str, Any]:
    return _task_view(session, task)


def get_task(session: Session, world: World, task_id: str) -> Task:
    return _get_task(session, world, task_id)


def list_tasks(
    session: Session,
    world: World,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[Task]:
    limit = max(1, min(int(limit), 500))
    query = select(Task).where(Task.world_id == world.id)
    if status:
        query = query.where(Task.status == status.upper())
    query = query.order_by(Task.created_at.desc()).limit(limit)
    return list(session.scalars(query).all())


def claim_task(session: Session, world: World, agent: Agent, task_id: str) -> Task:
    require_alive(session, agent, world)
    task = _get_task(session, world, task_id, lock=True)
    if task.status != TASK_OPEN:
        raise DomainError(f"task is not open ({task.status})", 409)
    if task.publisher_agent_id == agent.id:
        raise DomainError("cannot claim your own task", 409)
    if task.deadline_at is not None and task.deadline_at <= utcnow():
        raise DomainError("task deadline has passed", 409)
    task.assignee_agent_id = agent.id
    task.status = TASK_CLAIMED
    task.claimed_at = utcnow()
    _emit(
        session,
        world.id,
        "task.claimed",
        actor_agent_id=agent.id,
        data={"task_id": task.id, "title": task.title},
    )
    session.commit()
    session.refresh(task)
    return task


def submit_task(session: Session, world: World, agent: Agent, task_id: str, submission: str) -> Task:
    require_alive(session, agent, world)
    task = _get_task(session, world, task_id, lock=True)
    if task.status != TASK_CLAIMED or task.assignee_agent_id != agent.id:
        raise DomainError("only the current assignee can submit a claimed task", 409)
    if task.deadline_at is not None and task.deadline_at <= utcnow():
        raise DomainError("task deadline has passed", 409)
    submission = submission.strip()
    if not submission:
        raise DomainError("submission must not be empty", 400)
    task.submission = submission
    task.status = TASK_SUBMITTED
    task.submitted_at = utcnow()
    _emit(
        session,
        world.id,
        "task.submitted",
        actor_agent_id=agent.id,
        data={"task_id": task.id, "title": task.title},
    )
    session.commit()
    session.refresh(task)
    return task


def accept_task(
    session: Session,
    world: World,
    task_id: str,
    *,
    actor: Agent | None = None,
    is_admin: bool = False,
) -> Task:
    task = _get_task(session, world, task_id, lock=True)
    _authorize_task_manager(session, world, task, actor, is_admin=is_admin)
    if task.status != TASK_SUBMITTED:
        raise DomainError(f"task is not awaiting acceptance ({task.status})", 409)
    if task.assignee_agent_id is None:
        raise DomainError("task has no assignee", 500)
    assignee = session.get(Agent, task.assignee_agent_id)
    if assignee is None:
        raise DomainError("task assignee no longer exists", 500)
    if not refresh_alive(session, assignee, world):
        raise DomainError("assignee is dead; use reject/cancel instead", 409)
    escrow = _escrow_account(session, world.id)
    credit = _agent_account(session, assignee)
    locked = _locked_accounts(session, escrow.id, credit.id)
    escrow, credit = locked[escrow.id], locked[credit.id]
    if escrow.balance < task.reward:
        raise DomainError("escrow does not hold the task reward", 409)
    tx = _post_transfer(
        session,
        world,
        debit=escrow,
        credit=credit,
        amount=task.reward,
        kind="task_settlement",
        actor_agent_id=actor.id if actor else None,
        counterparty_agent_id=assignee.id,
        data={"task_id": task.id, "assignee": assignee.name},
    )
    task.status = TASK_COMPLETED
    task.completed_at = utcnow()
    task.closed_at = task.completed_at
    _emit(
        session,
        world.id,
        "task.completed",
        actor_agent_id=actor.id if actor else None,
        data={"task_id": task.id, "assignee": assignee.name, "transaction_id": tx.id},
    )
    session.commit()
    session.refresh(task)
    return task


def reject_task(
    session: Session,
    world: World,
    task_id: str,
    *,
    actor: Agent | None = None,
    is_admin: bool = False,
    reason: str | None = None,
) -> Task:
    task = _get_task(session, world, task_id, lock=True)
    _authorize_task_manager(session, world, task, actor, is_admin=is_admin)
    if task.status != TASK_SUBMITTED:
        raise DomainError(f"task is not awaiting acceptance ({task.status})", 409)
    task.status = TASK_OPEN
    task.assignee_agent_id = None
    task.submission = None
    task.claimed_at = None
    task.submitted_at = None
    _emit(
        session,
        world.id,
        "task.reopened",
        actor_agent_id=actor.id if actor else None,
        data={"task_id": task.id, "reason": (reason or "").strip()},
    )
    session.commit()
    session.refresh(task)
    return task


def cancel_task(
    session: Session,
    world: World,
    task_id: str,
    *,
    actor: Agent | None = None,
    is_admin: bool = False,
) -> Task:
    task = _get_task(session, world, task_id, lock=True)
    _authorize_task_manager(session, world, task, actor, is_admin=is_admin)
    if task.status != TASK_OPEN:
        raise DomainError("only an open task can be cancelled", 409)
    escrow = _escrow_account(session, world.id)
    funder = _task_funder_account(session, task)
    locked = _locked_accounts(session, escrow.id, funder.id)
    escrow, funder = locked[escrow.id], locked[funder.id]
    if escrow.balance < task.reward:
        raise DomainError("escrow does not hold the task reward", 409)
    tx = _post_transfer(
        session,
        world,
        debit=escrow,
        credit=funder,
        amount=task.reward,
        kind="task_refund",
        actor_agent_id=actor.id if actor else None,
        data={"task_id": task.id},
    )
    task.status = TASK_CANCELLED
    task.closed_at = utcnow()
    _emit(
        session,
        world.id,
        "task.cancelled",
        actor_agent_id=actor.id if actor else None,
        data={"task_id": task.id, "transaction_id": tx.id},
    )
    session.commit()
    session.refresh(task)
    return task


def expire_tasks(session: Session, world: World) -> int:
    now = utcnow()
    tasks = session.scalars(
        select(Task)
        .where(
            Task.world_id == world.id,
            Task.status.in_([TASK_OPEN, TASK_CLAIMED]),
            Task.deadline_at.is_not(None),
            Task.deadline_at <= now,
        )
        .with_for_update()
    ).all()
    count = 0
    escrow = _escrow_account(session, world.id)
    for task in tasks:
        funder = _task_funder_account(session, task)
        locked = _locked_accounts(session, escrow.id, funder.id)
        escrow_locked, funder_locked = locked[escrow.id], locked[funder.id]
        if escrow_locked.balance < task.reward:
            continue
        tx = _post_transfer(
            session,
            world,
            debit=escrow_locked,
            credit=funder_locked,
            amount=task.reward,
            kind="task_refund",
            actor_agent_id=None,
            data={"task_id": task.id, "expired": True},
        )
        task.status = TASK_CANCELLED if task.status == TASK_OPEN else TASK_FAILED
        task.closed_at = now
        _emit(
            session,
            world.id,
            "task.expired",
            data={
                "task_id": task.id,
                "status": task.status,
                "transaction_id": tx.id,
            },
        )
        count += 1
    if count:
        session.commit()
    return count



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
