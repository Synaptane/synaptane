"""Web sessions you can see and close: last activity, browser and address

Each web session keeps when it was last used (it expires after a period of inactivity), the
browser that opened it and its address, for the list in My account. Existing sessions count
as used now, with no browser or address known.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "web_sessions",
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "web_sessions",
        sa.Column("client", sa.String(length=300), server_default="", nullable=False),
    )
    op.add_column("web_sessions", sa.Column("address", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("web_sessions", "address")
    op.drop_column("web_sessions", "client")
    op.drop_column("web_sessions", "last_seen_at")
