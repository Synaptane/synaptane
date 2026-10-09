"""security

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sso_providers",
        sa.Column("link_by_email", sa.Boolean(), server_default="false", nullable=False),
    )
    # Existing providers that properly verify addresses: linking kept.
    op.execute("UPDATE sso_providers SET link_by_email = true WHERE kind IN ('github', 'google')")
    op.add_column("sso_states", sa.Column("link_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_sso_states_link_user_id_users"),
        "sso_states",
        "users",
        ["link_user_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_sso_states_link_user_id_users"), "sso_states", type_="foreignkey")
    op.drop_column("sso_states", "link_user_id")
    op.drop_column("sso_providers", "link_by_email")
