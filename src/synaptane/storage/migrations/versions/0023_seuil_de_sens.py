"""Semantic search: similarity floor per organization, next to the embedding model

Empty for existing organizations: the default rule (the best neighbour must stand out from
the median) stays in force.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "org_embedding_settings", sa.Column("meaning_min_similarity", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("org_embedding_settings", "meaning_min_similarity")
