"""Prometheus exposition with bounded labels and no per-agent identifiers."""

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from .config import utcnow
from .models import Agent, AgentRuntime, ExperimentRun, ModelGrant, World


def render(session):
    registry = CollectorRegistry()
    for name, table in (("worlds", World), ("agents", Agent)):
        gauge = Gauge("city_" + name, "Current stored " + name, registry=registry)
        gauge.set(session.scalar(select(func.count()).select_from(table)))
    alive = Gauge(
        "city_agents_alive", "Agents before their real-time death deadline", registry=registry
    )
    alive.set(
        session.scalar(select(func.count()).select_from(Agent).where(Agent.death_at > utcnow()))
    )
    for name, table, statuses in (
        (
            "experiments",
            ExperimentRun,
            ("QUEUED", "STARTING", "RUNNING", "STOPPING", "STOPPED", "COMPLETED", "FAILED"),
        ),
        ("runtimes", AgentRuntime, ("RUNNING", "SUSPENDED", "STOPPED", "CRASHED", "LOST")),
    ):
        gauge = Gauge(
            "city_" + name + "_by_status", "Current " + name, ["status"], registry=registry
        )
        counts = dict(
            session.execute(select(table.status, func.count()).group_by(table.status)).all()
        )
        for status in statuses:
            gauge.labels(status=status).set(counts.get(status, 0))
    grants = Gauge(
        "city_model_grants",
        "Stored virtual-key grants awaiting expiry or revocation",
        registry=registry,
    )
    grants.set(session.scalar(select(func.count()).select_from(ModelGrant)))
    return generate_latest(registry)
