"""SSO: allowed email domains per provider

Existing providers keep their automatic account creation setting; only the default for new
ones changes (disabled, in the model and the forms). Empty list: no restriction, as before.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sso_providers",
        sa.Column(
            "allowed_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("sso_providers", "allowed_domains")
