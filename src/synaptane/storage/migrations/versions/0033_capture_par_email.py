"""Capture by email: the mailbox the worker reads, and the emails it handled

`capture_settings` (a single row, empty until the super admin configures it) holds the IMAP
mailbox and how senders are authenticated; its password is encrypted with the master key.
`email_captures` remembers each email handled (a digest of its Message-ID, never its content):
the poll does not drop the same email twice, and the health page shows the refusals. No
existing row changes.

Revision ID: 0033
Revises: 0032
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capture_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sa.String(length=16), nullable=False),
        sa.Column("username", sa.String(length=320), nullable=False),
        sa.Column("password", sa.LargeBinary(), nullable=True),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("authserv_ids", sa.String(length=1000), nullable=False),
        sa.Column("unnamed_results", sa.Boolean(), nullable=False),
        sa.Column("spf_domains", sa.String(length=1000), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("replies", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_error", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_capture_settings_single_row")),
        sa.CheckConstraint(
            "security IN ('starttls', 'ssl', 'none')", name=op.f("ck_capture_settings_security")
        ),
        sa.CheckConstraint(
            "replies IN ('off', 'refusals', 'all')", name=op.f("ck_capture_settings_replies")
        ),
        sa.CheckConstraint("interval BETWEEN 1 AND 60", name=op.f("ck_capture_settings_interval")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture_settings")),
    )
    op.create_table(
        "email_captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("sender_domain", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=130), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('accepted', 'refused')", name=op.f("ck_email_captures_status")
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_email_captures_space_id_spaces"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_captures_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_captures")),
        sa.UniqueConstraint("key", name=op.f("uq_email_captures_key")),
    )
    op.create_index(op.f("ix_email_captures_at"), "email_captures", ["at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_captures_at"), table_name="email_captures")
    op.drop_table("email_captures")
    op.drop_table("capture_settings")
