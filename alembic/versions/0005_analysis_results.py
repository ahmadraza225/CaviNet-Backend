"""AI analysis results on cases (Phase 5: M-05, M-06).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("probability_tb", sa.Float(), nullable=True))
        batch.add_column(sa.Column("confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("confidence_band", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("model_name", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("model_version", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("model_is_demo", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(sa.Column("processing_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("warnings", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("analysis_attempts", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        for column in (
            "analysis_attempts",
            "warnings",
            "processing_seconds",
            "model_is_demo",
            "model_version",
            "model_name",
            "confidence_band",
            "confidence",
            "probability_tb",
        ):
            batch.drop_column(column)
