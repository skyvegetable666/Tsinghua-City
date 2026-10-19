"""Bounded employer funding and member-authorized conversations."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import service
from .config import iso, utcnow
from .models import (
    Account,
    Conversation,
    ConversationMember,
    ConversationMessage,
    Employer,
)


def create_employer(session, world, name, budget):
    if session.scalar(
        select(Employer.id).where(Employer.world_id == world.id, Employer.name == name)
    ):
        raise service.DomainError("employer name already exists", 409)
    account = Account(
        id=service._id("ac_"), world_id=world.id, agent_id=None, kind="employer", balance=0
    )
    session.add(account)
    session.flush()
    employer = Employer(id=service._id("em_"), world_id=world.id, name=name, account_id=account.id)
    session.add(employer)
    session.flush()
    if budget:
        system = service._system_account(session, world.id)
        locked = service._locked_accounts(session, system.id, account.id)
        service._post_transfer(
            session,
            world,
            debit=locked[system.id],
            credit=locked[account.id],
            amount=budget,
            kind="employer_funding",
            actor_agent_id=None,
            data={"employer_id": employer.id},
        )
    service._emit(
        session,
        world.id,
        "employer.created",
        data={"employer_id": employer.id, "name": name, "budget": budget},
    )
    session.commit()
    return employer


def employer_view(session, employer):
    return {
        "id": employer.id,
        "name": employer.name,
        "world_id": employer.world_id,
        "balance": session.get(Account, employer.account_id).balance,
    }


def publish_employer_task(session, world, employer_id, fields):
    employer = session.scalar(
        select(Employer)
        .where(
            Employer.id == employer_id,
            Employer.world_id == world.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if employer is None:
        raise service.DomainError("employer not found", 404)
    return service._create_task(
        session, world, publisher=None, source="external", employer=employer, **fields
    )


def create_conversation(session, world, actor, name, members):
    agents = {actor.id: actor}
    for member in members:
        agent = service._get_agent(session, world.id, member)
        service.require_alive(session, agent, world)
        agents[agent.id] = agent
    group = Conversation(id=service._id("cv_"), world_id=world.id, name=name, created_at=utcnow())
    session.add(group)
    session.flush()
    session.add_all(
        ConversationMember(conversation_id=group.id, agent_id=agent_id) for agent_id in agents
    )
    service._emit(
        session,
        world.id,
        "conversation.created",
        actor_agent_id=actor.id,
        data={"conversation_id": group.id, "members": list(agents)},
    )
    session.commit()
    return {"id": group.id, "name": group.name, "members": [a.name for a in agents.values()]}


def authorize_conversation(session, world, actor, group_id):
    group = session.get(Conversation, group_id)
    if group is None or group.world_id != world.id:
        raise service.DomainError("conversation not found", 404)
    if session.get(ConversationMember, (group_id, actor.id)) is None:
        raise service.DomainError("conversation membership required", 403)
    return group


def send_group_message(session, world, actor, group_id, body, client_message_id=None):
    authorize_conversation(session, world, actor, group_id)
    if len(body) > service._world_config(world).max_message_chars:
        raise service.DomainError("message is too long", 400)
    key = service._request_id(client_message_id)
    if key:
        existing = session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == group_id,
                ConversationMessage.sender_agent_id == actor.id,
                ConversationMessage.client_message_id == key,
            )
        )
        if existing:
            if existing.body != body:
                raise service.DomainError("client_message_id reused with different content", 409)
            return group_message_view(existing)
    row = ConversationMessage(
        id=service._id("gm_"),
        conversation_id=group_id,
        sender_agent_id=actor.id,
        body=body,
        client_message_id=key,
        created_at=utcnow(),
    )
    session.add(row)
    service._emit(
        session,
        world.id,
        "conversation.message",
        actor_agent_id=actor.id,
        data={"conversation_id": group_id, "message_id": row.id},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise service.DomainError("concurrent message conflict; retry the same id", 409) from exc
    return group_message_view(row)


def group_message_view(row):
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "sender_agent_id": row.sender_agent_id,
        "body": row.body,
        "created_at": iso(row.created_at),
    }
