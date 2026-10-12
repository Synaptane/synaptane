"""git sync and jobs

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name=op.f("ck_jobs_status")
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_jobs_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after"], unique=False)
    op.create_index(
        "uq_jobs_queued",
        "jobs",
        ["kind", "space_id"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.add_column(
        "spaces",
        sa.Column("sync_status", sa.String(length=16), nullable=False, server_default="never"),
    )
    op.add_column("spaces", sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("spaces", sa.Column("sync_error", sa.Text(), nullable=True))
    op.add_column("spaces", sa.Column("remote_head", sa.String(length=64), nullable=True))
    op.add_column(
        "spaces",
        sa.Column(
            "conflict_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_check_constraint(
        "sync_status", "spaces", "sync_status IN ('never', 'ok', 'error', 'conflict')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spaces_sync_status"), "spaces", type_="check")
    op.drop_column("spaces", "conflict_paths")
    op.drop_column("spaces", "remote_head")
    op.drop_column("spaces", "sync_error")
    op.drop_column("spaces", "synced_at")
    op.drop_column("spaces", "sync_status")
    op.drop_index(
        "uq_jobs_queued", table_name="jobs", postgresql_where=sa.text("status = 'queued'")
    )
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
