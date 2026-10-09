"""email verification

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Existing addresses: considered verified (instance in service).
    op.execute("UPDATE users SET email_verified_at = now() WHERE email IS NOT NULL")
    op.create_table(
        "email_checks",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_checks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_email_checks")),
    )
    op.create_index(op.f("ix_email_checks_user_id"), "email_checks", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_email_checks_user_id"), table_name="email_checks")
    op.drop_table("email_checks")
    op.drop_column("users", "email_verified_at")
