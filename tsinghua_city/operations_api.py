"""Experiment, funding, and conversation HTTP contracts."""

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from . import communities, service
from . import experiment_registry as registry
from .experiments import ExperimentManifest
from .models import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    Employer,
    ExperimentRun,
)
from .schemas import TaskCreate


class ExperimentSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: ExperimentManifest
    repeat: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def batch_size(self):
        if len(self.manifest.agents) * self.repeat > 100:
            raise ValueError("a batch may contain at most 100 agent runtimes")
        return self


class EmployerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    budget: int = Field(ge=0)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    members: list[str] = Field(default_factory=list, max_length=100)


class GroupMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


def router(settings, factory, manager, require_admin, require_agent, get_db):
    routes = APIRouter()
    admin = [Depends(require_admin)]

    def get_run(session, run_id):
        row = session.get(ExperimentRun, run_id)
        if row is None:
            raise service.DomainError("experiment not found", 404)
        return row

    @routes.get("/experiments", include_in_schema=False)
    def experiments_page():
        return FileResponse(Path(__file__).with_name("static") / "experiments.html")

    @routes.post("/v1/experiments", dependencies=admin, status_code=202)
    def submit(body: ExperimentSubmit, session=Depends(get_db)):
        return [
            registry.view(row)
            for row in registry.enqueue(
                session,
                settings,
                body.manifest,
                body.repeat,
            )
        ]

    @routes.get("/v1/experiments", dependencies=admin)
    def experiments(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), session=Depends(get_db)
    ):
        return [
            registry.view(row)
            for row in session.scalars(
                select(ExperimentRun)
                .order_by(
                    ExperimentRun.created_at.desc(),
                    ExperimentRun.id,
                )
                .offset(offset)
                .limit(limit)
            )
        ]

    @routes.get("/v1/experiments/compare", dependencies=admin)
    def compare(ids: str, session=Depends(get_db)):
        selected = list(dict.fromkeys(ids.split(",")))
        if not 1 <= len(selected) <= 20:
            raise service.DomainError("compare between 1 and 20 experiments", 400)
        return [
            {
                "run": registry.view(get_run(session, run_id)),
                "metrics": registry.metrics(session, settings, get_run(session, run_id)),
            }
            for run_id in selected
        ]

    @routes.get("/v1/experiments/{run_id}", dependencies=admin)
    def experiment(run_id: str, session=Depends(get_db)):
        row = get_run(session, run_id)
        return {**registry.view(row), "metrics": registry.metrics(session, settings, row)}

    @routes.post("/v1/experiments/{run_id}/stop", dependencies=admin)
    def stop_experiment(run_id: str, session=Depends(get_db)):
        return registry.view(registry.request_stop(session, get_run(session, run_id)))

    @routes.get("/v1/experiments/{run_id}/export", dependencies=admin)
    def export(run_id: str, format: str = "json", session=Depends(get_db)):
        row = get_run(session, run_id)
        if format == "json":
            return JSONResponse(
                registry.export_run(session, settings, row),
                headers={
                    "Content-Disposition": f'attachment; filename="{row.id}.json"',
                },
            )
        if format != "csv":
            raise service.DomainError("format must be json or csv", 400)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["experiment_id", "metric", "value"])
        for key, value in registry.metrics(session, settings, row).items():
            if isinstance(value, (int, float)):
                writer.writerow([row.id, key, value])
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{row.id}.csv"',
            },
        )

    @routes.post("/v1/worlds/{world_id}/employers", dependencies=admin, status_code=201)
    def create_employer(world_id: str, body: EmployerCreate, session=Depends(get_db)):
        world = service._get_world(session, world_id)
        return communities.employer_view(
            session,
            communities.create_employer(
                session,
                world,
                body.name,
                body.budget,
            ),
        )

    @routes.get("/v1/worlds/{world_id}/employers", dependencies=admin)
    def employers(world_id: str, session=Depends(get_db)):
        service._get_world(session, world_id)
        return [
            communities.employer_view(session, row)
            for row in session.scalars(select(Employer).where(Employer.world_id == world_id))
        ]

    @routes.post(
        "/v1/worlds/{world_id}/employers/{employer_id}/tasks", dependencies=admin, status_code=201
    )
    def employer_task(world_id: str, employer_id: str, body: TaskCreate, session=Depends(get_db)):
        world = service._get_world(session, world_id)
        task = communities.publish_employer_task(session, world, employer_id, body.model_dump())
        return service.task_view(session, task)

    @routes.post("/v1/me/conversations", status_code=201)
    def create_conversation(body: ConversationCreate, auth=Depends(require_agent)):
        return communities.create_conversation(
            auth.session, auth.world, auth.agent, body.name, body.members
        )

    @routes.get("/v1/me/conversations")
    def conversations(auth=Depends(require_agent)):
        rows = auth.session.scalars(
            select(Conversation)
            .join(ConversationMember)
            .where(
                ConversationMember.agent_id == auth.agent.id,
                Conversation.world_id == auth.world.id,
            )
            .order_by(Conversation.created_at)
            .limit(200)
        )
        return [{"id": row.id, "name": row.name} for row in rows]

    @routes.post("/v1/me/conversations/{group_id}/messages", status_code=201)
    def group_send(group_id: str, body: GroupMessageCreate, auth=Depends(require_agent)):
        return communities.send_group_message(
            auth.session, auth.world, auth.agent, group_id, body.body, body.client_message_id
        )

    @routes.get("/v1/me/conversations/{group_id}/messages")
    def group_read(
        group_id: str,
        after: float = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        auth=Depends(require_agent),
    ):
        communities.authorize_conversation(auth.session, auth.world, auth.agent, group_id)
        rows = auth.session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == group_id,
                ConversationMessage.created_at > after,
            )
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
            .limit(limit)
        )
        return [communities.group_message_view(row) for row in rows]

    return routes
