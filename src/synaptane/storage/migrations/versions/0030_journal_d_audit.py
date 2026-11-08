"""Audit log: index on (organization, date) of the audit events

An organization's audit log reads its events newest first: this index gives them in that
order, without sorting all the organization's events. Each page still reads the events it
skips (page changes, inbox notes, costs…): when they far outnumber the audit events, a page,
the first one and the export included, goes through up to all the organization's events (about
15 to 40 ms for 300,000 of them). It replaces the index on the organization alone, which it
covers.

Revision ID: 0030
Revises: 0028
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_audit_events_org_id_at", "audit_events", ["org_id", "at"], unique=False)
    op.drop_index("ix_audit_events_org_id", table_name="audit_events")


def downgrade() -> None:
    op.create_index("ix_audit_events_org_id", "audit_events", ["org_id"], unique=False)
    op.drop_index("ix_audit_events_org_id_at", table_name="audit_events")
