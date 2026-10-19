"""World API.

Admin routes create worlds and agents. Agent routes are authenticated by the
per-agent bearer token and only operate on that agent's own state. The runtime
adapter and MCP server are clients of this API, not owners of world state.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .auth import admin_matches, extract_bearer
from .config import Settings, load_settings
from .db import init_db, make_engine, make_session_factory
from .models import Agent, World
from .runtime import RuntimeManager
from .scheduler import Scheduler
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
    OperatorMessageCreate,
    OperatorMessageOut,
    OperatorReadRequest,
    PostCreate,
    PostDetailOut,
    PostOut,
    ReplyCreate,
    ReplyOut,
    RuntimeOut,
    RuntimeStart,
    StateOut,
    TaskCreate,
    TaskOut,
    TaskReject,
    TaskSubmit,
    TransferRequest,
    WorldCreate,
    WorldOut,
)

CANVAS_PATH = Path(__file__).with_name("static") / "canvas.html"
TRACES_PATH = Path(__file__).with_name("static") / "traces.html"


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
    scheduler: bool | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    run_scheduler = settings.scheduler_enabled if scheduler is None else scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        runtime_manager.reconcile()
        maintenance = (
            Scheduler(
                session_factory,
                settings.scheduler_interval_seconds,
                runtime_manager=runtime_manager,
            )
            if run_scheduler
            else None
        )
        if maintenance is not None:
            maintenance.start()
        try:
            yield
        finally:
            if maintenance is not None:
                maintenance.stop()

    app = FastAPI(
        title="Tsinghua City",
        version="0.1.0",
        description="Authoritative world state for agent-society experiments.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    runtime_manager = RuntimeManager(settings, session_factory)
    app.state.runtime_manager = runtime_manager

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

    @app.get("/canvas", include_in_schema=False)
    def canvas() -> FileResponse:
        return FileResponse(CANVAS_PATH, media_type="text/html")

    @app.get("/traces", include_in_schema=False)
    def traces_page() -> FileResponse:
        return FileResponse(TRACES_PATH, media_type="text/html")

    def trace_root() -> Path:
        return Path(settings.trace_dir).expanduser().resolve()

    @app.get("/v1/traces")
    def list_traces(
        world_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        experiment_id: str | None = None,
        _admin: None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        root = trace_root()
        if not root.is_dir():
            return []
        rows = []
        for path in sorted(
            root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True
        ):
            if not path.is_file():
                continue
            if path.is_symlink():
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    metadata = json.loads(handle.readline())
            except (OSError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            filters = {
                "world_id": world_id, "agent_id": agent_id,
                "run_id": run_id, "experiment_id": experiment_id,
            }
            if any(value is not None and metadata.get(key) != value
                   for key, value in filters.items()):
                continue
            stat = path.stat()
            rows.append({
                "name": path.name, "size": stat.st_size, "modified": stat.st_mtime,
                **{key: metadata.get(key) for key in filters},
            })
            if len(rows) >= 200:
                break
        return rows

    @app.get("/v1/traces/{name}")
    def read_trace(
        name: str,
        limit: int = Query(default=5000, ge=1, le=20000),
        offset: int = Query(default=0, ge=0),
        _admin: None = Depends(require_admin),
    ) -> dict[str, Any]:
        if Path(name).name != name or not name.endswith(".jsonl"):
            raise HTTPException(status_code=404, detail="trace not found")
        path = trace_root() / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(status_code=404, detail="trace not found")
        events: list[Any] = []
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < offset:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"type": "raw", "text": line})
                if len(events) >= limit:
                    return {"name": name, "events": events, "next_offset": index + 1}
        return {"name": name, "events": events, "next_offset": None}

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

    @app.get("/v1/worlds/{world_id}/posts", response_model=list[PostOut])
    def admin_list_posts(
        world_id: str,
        limit: int = Query(default=100, ge=1, le=200),
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return [
            service.post_view(session, post)
            for post in service.list_posts(session, world, limit=limit)
        ]

    @app.get("/v1/worlds/{world_id}/posts/{post_id}", response_model=PostDetailOut)
    def admin_get_post(
        world_id: str,
        post_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        post = service.get_post(session, world, post_id)
        return service.post_view(session, post, include_replies=True)

    def runtime_subject(
        world_id: str,
        agent_id: str,
        session: Session,
    ) -> tuple[World, Agent]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        agent = session.get(Agent, agent_id)
        if agent is None or agent.world_id != world.id:
            raise HTTPException(status_code=404, detail="agent not found in world")
        return world, agent

    @app.get(
        "/v1/worlds/{world_id}/agents/{agent_id}/operator/messages",
        response_model=list[OperatorMessageOut],
    )
    def admin_list_operator_messages(
        world_id: str,
        agent_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        _world, agent = runtime_subject(world_id, agent_id, session)
        return [
            service.operator_message_view(message)
            for message in service.list_operator_messages(session, agent, limit=limit)
        ]

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/operator/messages",
        response_model=OperatorMessageOut,
        status_code=201,
    )
    def admin_send_operator_message(
        world_id: str,
        agent_id: str,
        body: OperatorMessageCreate,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        message = service.send_operator_message(session, world, agent, body.body)
        return service.operator_message_view(message)

    @app.get(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime",
        response_model=RuntimeOut,
    )
    def get_runtime(
        world_id: str,
        agent_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        return runtime_manager.view(runtime_manager.status(session, world, agent))

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime/start",
        response_model=RuntimeOut,
    )
    def start_runtime(
        world_id: str,
        agent_id: str,
        body: RuntimeStart,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        runtime = runtime_manager.start(
            session,
            world,
            agent,
            command=body.command,
            adapter=body.adapter,
            restart_policy=body.restart_policy,
            max_restarts=body.max_restarts,
        )
        return runtime_manager.view(runtime)

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime/stop",
        response_model=RuntimeOut,
    )
    def stop_runtime(
        world_id: str,
        agent_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        return runtime_manager.view(runtime_manager.stop(session, world, agent))

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime/suspend",
        response_model=RuntimeOut,
    )
    def suspend_runtime(
        world_id: str,
        agent_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        return runtime_manager.view(runtime_manager.suspend(session, world, agent))

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime/resume",
        response_model=RuntimeOut,
    )
    def resume_runtime(
        world_id: str,
        agent_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        return runtime_manager.view(runtime_manager.resume(session, world, agent))

    @app.post(
        "/v1/worlds/{world_id}/agents/{agent_id}/runtime/terminate",
        response_model=RuntimeOut,
    )
    def terminate_runtime(
        world_id: str,
        agent_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world, agent = runtime_subject(world_id, agent_id, session)
        return runtime_manager.view(runtime_manager.terminate(session, world, agent))

    @app.get("/v1/worlds/{world_id}/runtimes", response_model=list[RuntimeOut])
    def list_runtimes(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return [
            runtime_manager.view(runtime)
            for runtime in runtime_manager.list_world(session, world)
        ]

    @app.post("/v1/worlds/{world_id}/runtimes/reconcile")
    def reconcile_runtimes(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        if session.get(World, world_id) is None:
            raise HTTPException(status_code=404, detail="world not found")
        return {"reconciled": runtime_manager.reconcile()}

    @app.post(
        "/v1/worlds/{world_id}/tasks",
        response_model=TaskOut,
        status_code=201,
    )
    def create_external_task(
        world_id: str,
        body: TaskCreate,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        task = service.publish_external_task(
            session,
            world,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            reward=body.reward,
            deadline_seconds=body.deadline_seconds,
            client_task_id=body.client_task_id,
        )
        return service.task_view(session, task)

    @app.get("/v1/worlds/{world_id}/tasks", response_model=list[TaskOut])
    def admin_list_tasks(
        world_id: str,
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=500),
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return [
            service.task_view(session, task)
            for task in service.list_tasks(session, world, status=status, limit=limit)
        ]

    @app.post("/v1/worlds/{world_id}/tasks/{task_id}/accept", response_model=TaskOut)
    def admin_accept_task(
        world_id: str,
        task_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        task = service.accept_task(session, world, task_id, is_admin=True)
        return service.task_view(session, task)

    @app.post("/v1/worlds/{world_id}/tasks/{task_id}/reject", response_model=TaskOut)
    def admin_reject_task(
        world_id: str,
        task_id: str,
        body: TaskReject | None = None,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        task = service.reject_task(
            session,
            world,
            task_id,
            is_admin=True,
            reason=body.reason if body else None,
        )
        return service.task_view(session, task)

    @app.post("/v1/worlds/{world_id}/tasks/{task_id}/cancel", response_model=TaskOut)
    def admin_cancel_task(
        world_id: str,
        task_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        task = service.cancel_task(session, world, task_id, is_admin=True)
        return service.task_view(session, task)

    @app.post("/v1/worlds/{world_id}/tasks/expire")
    def admin_expire_tasks(
        world_id: str,
        _admin: None = Depends(require_admin),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        world = session.get(World, world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="world not found")
        return {"expired": service.expire_tasks(session, world)}

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

    @app.get("/v1/me/operator/messages", response_model=list[OperatorMessageOut])
    def get_operator_messages(
        limit: int = Query(default=50, ge=1, le=200),
        auth: AgentAuth = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        return [
            service.operator_message_view(message)
            for message in service.list_operator_messages(
                auth.session, auth.agent, limit=limit, from_operator=True
            )
        ]

    @app.post(
        "/v1/me/operator/messages",
        response_model=OperatorMessageOut,
        status_code=201,
    )
    def reply_to_operator(
        body: OperatorMessageCreate,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        message = service.send_agent_operator_message(
            auth.session, auth.world, auth.agent, body.body
        )
        return service.operator_message_view(message)

    @app.post("/v1/me/operator/messages/read")
    def mark_operator_read(
        body: OperatorReadRequest,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        changed = service.mark_operator_messages_read(
            auth.session, auth.agent, body.message_ids
        )
        return {"ok": True, "changed": changed}

    @app.get("/v1/me/posts", response_model=list[PostOut])
    def list_posts(
        limit: int = Query(default=100, ge=1, le=200),
        auth: AgentAuth = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        return [
            service.post_view(auth.session, post)
            for post in service.list_posts(auth.session, auth.world, limit=limit)
        ]

    @app.get("/v1/me/posts/{post_id}", response_model=PostDetailOut)
    def get_post(
        post_id: str,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        post = service.get_post(auth.session, auth.world, post_id)
        return service.post_view(auth.session, post, include_replies=True)

    @app.post("/v1/me/posts", response_model=PostOut, status_code=201)
    def publish_post(
        body: PostCreate,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        post = service.publish_post(
            auth.session,
            auth.world,
            auth.agent,
            title=body.title,
            body=body.body,
        )
        return service.post_view(auth.session, post)

    @app.post(
        "/v1/me/posts/{post_id}/replies",
        response_model=ReplyOut,
        status_code=201,
    )
    def reply_to_post(
        post_id: str,
        body: ReplyCreate,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        reply = service.reply_to_post(
            auth.session,
            auth.world,
            auth.agent,
            post_id,
            body.body,
        )
        return service.reply_view(auth.session, reply)

    @app.get("/v1/me/tasks", response_model=list[TaskOut])
    def list_tasks(
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=500),
        auth: AgentAuth = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        tasks = service.list_tasks(auth.session, auth.world, status=status, limit=limit)
        return [service.task_view(auth.session, task) for task in tasks]

    @app.get("/v1/me/tasks/{task_id}", response_model=TaskOut)
    def get_task(
        task_id: str,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.get_task(auth.session, auth.world, task_id)
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks", response_model=TaskOut, status_code=201)
    def publish_task(
        body: TaskCreate,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.publish_task(
            auth.session,
            auth.world,
            auth.agent,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            reward=body.reward,
            deadline_seconds=body.deadline_seconds,
            client_task_id=body.client_task_id,
        )
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks/{task_id}/claim", response_model=TaskOut)
    def claim_task(
        task_id: str,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.claim_task(auth.session, auth.world, auth.agent, task_id)
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks/{task_id}/submit", response_model=TaskOut)
    def submit_task(
        task_id: str,
        body: TaskSubmit,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.submit_task(
            auth.session,
            auth.world,
            auth.agent,
            task_id,
            body.submission,
        )
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks/{task_id}/accept", response_model=TaskOut)
    def accept_task(
        task_id: str,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.accept_task(
            auth.session,
            auth.world,
            task_id,
            actor=auth.agent,
        )
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks/{task_id}/reject", response_model=TaskOut)
    def reject_task(
        task_id: str,
        body: TaskReject | None = None,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.reject_task(
            auth.session,
            auth.world,
            task_id,
            actor=auth.agent,
            reason=body.reason if body else None,
        )
        return service.task_view(auth.session, task)

    @app.post("/v1/me/tasks/{task_id}/cancel", response_model=TaskOut)
    def cancel_task(
        task_id: str,
        auth: AgentAuth = Depends(require_agent),
    ) -> dict[str, Any]:
        task = service.cancel_task(
            auth.session,
            auth.world,
            task_id,
            actor=auth.agent,
        )
        return service.task_view(auth.session, task)

    return app


app = create_app()
