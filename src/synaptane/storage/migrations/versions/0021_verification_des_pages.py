"""page review period, per space

Each page's fields (owner, verification date, review period) are in its front matter, hence
in Git; only the space's default review period is a setting.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spaces",
        sa.Column("review_every", sa.String(length=16), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("spaces", "review_every")
