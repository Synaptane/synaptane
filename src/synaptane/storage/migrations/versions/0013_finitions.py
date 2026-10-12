"""finishing touches

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "org_ai_settings", sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("org_ai_settings", sa.Column("test_ok", sa.Boolean(), nullable=True))
    op.add_column(
        "org_ai_settings",
        sa.Column("test_message", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("org_ai_settings", "test_message")
    op.drop_column("org_ai_settings", "test_ok")
    op.drop_column("org_ai_settings", "tested_at")
