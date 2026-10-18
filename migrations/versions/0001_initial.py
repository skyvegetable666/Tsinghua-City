"""Initial world schema.

The baseline intentionally delegates to the SQLAlchemy metadata so the very
first schema matches the Phase 1/2 models exactly. Subsequent revisions should
use explicit Alembic operations.
"""

from __future__ import annotations

from alembic import op

from tsinghua_city.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
