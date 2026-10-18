"""Postgres-only concurrency checks.

These are skipped unless CITY_TEST_DATABASE_URL points at a disposable Postgres
database. SQLite cannot provide meaningful row-lock behavior here.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import create_agent, create_world
from sqlalchemy import func, select

from tsinghua_city import service
from tsinghua_city.models import Account, Agent, LedgerTransaction, World

pytestmark = pytest.mark.skipif(
    not os.environ.get("CITY_TEST_DATABASE_URL"),
    reason="requires CITY_TEST_DATABASE_URL (disposable Postgres)",
)


def _run_transfer(factory, world_id, agent_id, amount, barrier, key=None):
    with factory() as session:
        world = session.get(World, world_id)
        agent = session.get(Agent, agent_id)
        barrier.wait()
        try:
            service.transfer(
                session,
                world,
                agent,
                to="bob",
                amount=amount,
                client_request_id=key,
            )
            return "ok"
        except service.DomainError as exc:
            session.rollback()
            return exc.status


def test_concurrent_transfer_does_not_double_spend(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    create_agent(client, admin_headers, world["id"], "bob")
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _run_transfer(
                    session_factory,
                    world["id"],
                    alice["id"],
                    80,
                    barrier,
                ),
                range(2),
            )
        )

    assert sorted(str(item) for item in results) == ["409", "ok"]
    with session_factory() as session:
        balance = session.scalar(
            select(Account.balance).join(Agent, Account.agent_id == Agent.id).where(Agent.name == "alice")
        )
        assert balance == 20
        assert session.scalar(select(func.sum(Account.balance))) == 0


def test_concurrent_same_idempotency_key_is_single_transfer(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    create_agent(client, admin_headers, world["id"], "bob")
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _run_transfer(
                    session_factory,
                    world["id"],
                    alice["id"],
                    10,
                    barrier,
                    key="concurrent-key",
                ),
                range(2),
            )
        )

    assert results == ["ok", "ok"]
    with session_factory() as session:
        alice_balance = session.scalar(
            select(Account.balance).join(Agent, Account.agent_id == Agent.id).where(Agent.name == "alice")
        )
        assert alice_balance == 90
        assert session.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.kind == "transfer"
            )
        ) == 1
