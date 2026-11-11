"""External HTTP sources of the routines

`routine_sources` keeps the sources the administrators declare in a space: a name, a base
address (HTTPS, host, path prefix) and an optional secret, encrypted and bound to the source
and its origin. `routines.sources` is the copy of what a routine's file chooses among them
(`sources:`), empty for every existing routine. No existing value changes.

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routine_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("auth", sa.String(length=8), nullable=False),
        sa.Column("header", sa.String(length=64), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=True),
        sa.Column("secret_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "auth IN ('', 'bearer', 'header', 'basic')", name=op.f("ck_routine_sources_auth")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_routine_sources_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_routine_sources_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_sources")),
        sa.UniqueConstraint("space_id", "name", name=op.f("uq_routine_sources_space_id")),
    )
    op.add_column(
        "routines",
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("routines", "sources")
    op.drop_table("routine_sources")
