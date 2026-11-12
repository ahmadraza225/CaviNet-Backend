"""Symptoms entered at upload (Phase 11: M-12, FR-12.1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("symptoms", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("symptoms")
