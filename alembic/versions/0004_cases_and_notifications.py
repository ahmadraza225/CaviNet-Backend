"""Cases, case timeline and notifications (Phase 4: M-04, M-08).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'uploaded', 'validating', 'queued', 'preprocessing', 'analysing', 'completed', 'failed'"
BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("upload_kind", sa.String(length=8), nullable=False),
        sa.Column("upload_files", sa.Integer(), nullable=False),
        sa.Column("upload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("series_found", sa.Integer(), nullable=True),
        sa.Column("series_number", sa.Integer(), nullable=True),
        sa.Column("study_date", sa.Date(), nullable=True),
        sa.Column("num_slices", sa.Integer(), nullable=True),
        sa.Column("slice_thickness_mm", sa.Float(), nullable=True),
        sa.Column("slice_spacing_mm", sa.Float(), nullable=True),
        sa.Column("pixel_spacing_row_mm", sa.Float(), nullable=True),
        sa.Column("pixel_spacing_col_mm", sa.Float(), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("columns", sa.Integer(), nullable=True),
        sa.Column("manufacturer", sa.String(length=64), nullable=True),
        sa.Column("manufacturer_model", sa.String(length=64), nullable=True),
        sa.Column("convolution_kernel", sa.String(length=64), nullable=True),
        sa.Column("result_label", sa.String(length=16), nullable=True),
        sa.Column("result_is_stub", sa.Boolean(), nullable=False),
        sa.Column("analyser", sa.String(length=64), nullable=True),
        sa.Column("result_details", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_cases_status_valid")),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            name=op.f("fk_cases_patient_id_patients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_cases_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
    )
    op.create_index(op.f("ix_cases_patient_id"), "cases", ["patient_id"])
    op.create_index(op.f("ix_cases_status"), "cases", ["status"])
    op.create_index(op.f("ix_cases_uploaded_by_id"), "cases", ["uploaded_by_id"])

    op.create_table(
        "case_events",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_case_events_case_id_cases"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_events")),
    )
    op.create_index(op.f("ix_case_events_case_id"), "case_events", ["case_id"])

    op.create_table(
        "notifications",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_notifications_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(op.f("ix_notifications_case_id"), "notifications", ["case_id"])
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_case_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(op.f("ix_case_events_case_id"), table_name="case_events")
    op.drop_table("case_events")
    op.drop_index(op.f("ix_cases_uploaded_by_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_status"), table_name="cases")
    op.drop_index(op.f("ix_cases_patient_id"), table_name="cases")
    op.drop_table("cases")
