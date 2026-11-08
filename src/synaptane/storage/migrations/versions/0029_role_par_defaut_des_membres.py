"""Default role of an organization's members in its spaces, and restricted spaces

Per organization, the role each member has on every space without being added to it:
`none`, `reader` or `contributor`. Per space, `restricted`: the default role does not apply
there. Existing organizations get `none` and existing spaces are not restricted: access is
unchanged.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "default_space_role", sa.String(length=16), server_default="none", nullable=False
        ),
    )
    op.create_check_constraint(
        "default_space_role",
        "organizations",
        "default_space_role IN ('none', 'reader', 'contributor')",
    )
    op.add_column(
        "spaces",
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("spaces", "restricted")
    op.drop_constraint(op.f("ck_organizations_default_space_role"), "organizations", type_="check")
    op.drop_column("organizations", "default_space_role")
