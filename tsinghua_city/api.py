"""World API.

Admin routes create worlds and agents. Agent routes are authenticated by the
per-agent bearer token and only operate on that agent's own state. The runtime
adapter and MCP server are clients of this API, not owners of world state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .auth import admin_matches, extract_bearer
from .config import Settings, load_settings
from .db import init_db, make_engine, make_session_factory
from .models import Agent, World
from .schemas import (
    AgentCreate,
    AgentCreated,
    AgentOut,
    BalanceOut,
    EventOut,
    ExtendLifeRequest,
    HealthOut,
    MarkReadRequest,
    MessageCreate,
    MessageOut,
    StateOut,
    TransferRequest,
    WorldCreate,
    WorldOut,
)


@dataclass
class AgentAuth:
    session: Session
    agent: Agent
    world: World


def _agent_output(session: Session, agent: Agent, token: str | None = None) -> dict[str, Any]:
    data = service.agent_view(session, agent)
    if token is not None:
        data["token"] = token
    return data


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        yield

    app = FastAPI(
        title="Tsinghua City",
        version="0.1.0",
        description="Authoritative world state for agent-society experiments.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    @app.exception_handler(service.DomainError)
    async def domain_error_handler(_request, exc: service.DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    def get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def require_admin(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        token = extract_bearer(authorization)
        if not admin_matches(token, request.app.state.settings.admin_token):
            raise HTTPException(status_code=401, detail="admin token required")

    def require_agent(
        request: Request,
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_db),
    ) -> AgentAuth:
        token = extract_bearer(authorization)
        if token is None:
            raise HTTPException(status_code=401, detail="agent token required")
        agent = service.agent_from_token(session, token)
        if agent is None:
            raise HTTPException(status_code=401, detail="invalid agent token")
        world = session.get(World, agent.world_id)
        if world is None:
            raise HTTPException(status_code=401, detail="agent world missing")
        if not service.refresh_alive(session, agent, world):
            raise HTTPException(status_code=409, detail="agent is dead")
        return AgentAuth(session=session, agent=agent, world=world)

    @app.get("/health", response_model=HealthOut)
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "tsinghua-city", "version": "0.1.0"}

    # ------------------------------------------------------------------ admin
    @app.post("/v1/worlds", response_model=WorldOut, status_code=201)
    def create_world(
        body: WorldCreate,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = service.create_world(session, body.name, body.config)
        return service.world_view(world)

    @app.get("/v1/worlds", response_model=list[WorldOut])
    def list_worlds(
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        worlds = session.scalars(select(World).order_by(World.created_at)).all()
        return [service.world_view(world) for world in worlds]

    @app.get("/v1/worlds/{world_id}", response_model=WorldOut)
    def get_world(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return service.world_view(world)

    @app.post(
        "/v1/worlds/{world_id}/agents",
        response_model=AgentCreated,
        status_code=201,
    )
    def create_agent(
        world_id: str,
        body: AgentCreate,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        agent, token = service.create_agent(
            session,
            world,
            body.name,
            initial_credits=body.initial_credits,
            initial_life_seconds=body.initial_life_seconds,
        )
        return _agent_output(session, agent, token)

    @app.get("/v1/worlds/{world_id}/agents", response_model=list[AgentOut])
    def admin_list_agents(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return [_agent_output(session, agent) for agent in service.list_agents(session, world)]

    @app.post("/v1/worlds/{world_id}/reap")
    def reap(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return {"reaped": service.reap_expired(session, world)}

    @app.get("/v1/worlds/{world_id}/events", response_model=list[EventOut])
    def world_events(
        world_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return [service.event_view(event) for event in service.events(session, world, limit=limit)]

    # ------------------------------------------------------------------ agent
    @app.get("/v1/me", response_model=StateOut)
    def me(auth: AgentAuth = Depends(require_agent)) -> dict[str, Any]:
        view = _agent_output(auth.session, auth.agent)
        return {"agent": view, "balance": view["balance"]}

    @app.get("/v1/me/balance", response_model=BalanceOut)
    def balance(auth: AgentAuth = Depends(require_agent)) -> dict[str, int]:
        return {"balance": service.account_balance(auth.session, auth.agent)}

    @app.post("/v1/me/extend-life")
    def extend_life(
        body: ExtendLifeRequest,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        result = service.extend_life(
            auth.session,
            auth.world,
            auth.agent,
            credits=body.credits,
            client_request_id=body.client_request_id,
        )
        return {"ok": True, **result}

    @app.post("/v1/me/transfer")
    def transfer(
        body: TransferRequest,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        tx = service.transfer(
            auth.session,
            auth.world,
            auth.agent,
            to=body.to,
            amount=body.amount,
            client_request_id=body.client_request_id,
        )
        return {"ok": True, "transaction_id": tx.id, "amount": tx.amount, "to": body.to}

    @app.get("/v1/me/agents", response_model=list[AgentOut])
    def peers(auth: AgentAuth = Depends(require_agent)) -> list[dict[str, Any]]:
        agents = service.list_agents(auth.session, auth.world, exclude_agent_id=auth.agent.id)
        return [_agent_output(auth.session, agent) for agent in agents]

    @app.post("/v1/me/messages", response_model=MessageOut, status_code=201)
    def send_message(
        body: MessageCreate,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        message = service.send_message(
            auth.session,
            auth.world,
            auth.agent,
            to=body.to,
            body=body.body,
            client_message_id=body.client_message_id,
        )
        return service.message_view(auth.session, message)

    @app.get("/v1/me/messages", response_model=list[MessageOut])
    def get_inbox(
        unread_only: bool = False,
        limit: int = Query(default=50, ge=1, le=200),
        auth: AgentAuth = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        messages = service.inbox(
            auth.session,
            auth.agent,
            unread_only=unread_only,
            limit=limit,
        )
        return [service.message_view(auth.session, message) for message in messages]

    @app.post("/v1/me/messages/read")
    def mark_read(
        body: MarkReadRequest,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        changed = service.mark_read(auth.session, auth.agent, body.message_ids)
        return {"ok": True, "changed": changed}

    return app


app = create_app()
