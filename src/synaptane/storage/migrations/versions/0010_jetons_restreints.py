"""restricted tokens

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

TABLES = ("api_tokens", "oauth_codes", "oauth_tokens")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table, sa.Column("read_only", sa.Boolean(), server_default="false", nullable=False)
        )
        op.add_column(
            table,
            sa.Column("space_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "space_ids")
        op.drop_column(table, "read_only")
