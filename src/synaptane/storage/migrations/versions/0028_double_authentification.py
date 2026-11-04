"""Two-factor authentication: the authenticator app's secret and the recovery codes

Per account: the TOTP secret (encrypted with the master key), when two-factor authentication
was turned on, the time step of the last code accepted, and the digests of the recovery
codes. Existing accounts have none of them: two-factor authentication off.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_step", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("recovery_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "recovery_codes")
    op.drop_column("users", "totp_step")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret")
