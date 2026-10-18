"""Regression: SELECT FOR UPDATE must refresh identity-map account state."""

from __future__ import annotations

from conftest import create_agent, create_world
from sqlalchemy import select, update

from tsinghua_city import service
from tsinghua_city.models import Account


def test_locked_accounts_refresh_stale_identity_map(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")

    with session_factory() as session:
        account = session.scalars(
            select(Account).where(Account.agent_id == alice["id"])
        ).one()
        account_id = account.id
        assert account.balance == 100

        # Simulate a balance changed by another committed transaction while
        # this session still has the old object in its identity map.
        session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(balance=20)
            .execution_options(synchronize_session=False)
        )

        locked = service._locked_accounts(session, account_id)
        assert locked[account_id].balance == 20
