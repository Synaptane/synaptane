"""Connections limited to the inbox: reading, and dropping notes, but no direct change

A third level next to "read only" and "read and write", for personal tokens and OAuth
connections. Existing ones keep their level (false).

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("inbox_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "oauth_codes",
        sa.Column("inbox_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "oauth_tokens",
        sa.Column("inbox_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("oauth_tokens", "inbox_only")
    op.drop_column("oauth_codes", "inbox_only")
    op.drop_column("api_tokens", "inbox_only")
