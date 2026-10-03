"""Patients (Phase 3: M-03).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("mr_number", sa.String(length=32), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("sex", sa.String(length=8), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sex IN ('male', 'female')", name=op.f("ck_patients_sex_valid")),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_patients_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patients")),
    )
    op.create_index(op.f("ix_patients_mr_number"), "patients", ["mr_number"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_patients_mr_number"), table_name="patients")
    op.drop_table("patients")
