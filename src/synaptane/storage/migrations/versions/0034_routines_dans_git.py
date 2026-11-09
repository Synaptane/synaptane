"""Routines described by files of the repository (`.synaptane/routines/<name>.md`)

`routines.name` becomes the file name (lowercase letters, digits and dashes, unique in the
space); the former name, free text, becomes `title`. `sha` is the hash of the file at the
last reconciliation: NULL for every existing routine, which the next synchronization of its
space writes to the repository in one commit in Synaptane's name (`routine_files`), keeping
its row, hence its state (`last_date`, `last_run_at`, `requested`) and its runs. `error` and
`warnings` say what is wrong with a file.

Revision ID: 0034
Revises: 0033
"""

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ß": "ss", "ø": "o", "đ": "d", "ł": "l"})


def _file_name(title: str, taken: set[str]) -> str:
    """Same rule as `routine_files.name_for`, then `-2`, `-3`… if the name is taken. Written
    again here: a migration does not depend on code that will change."""
    folded = unicodedata.normalize("NFKD", title.lower().translate(_LIGATURES))
    text = "".join(c for c in folded if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-") or "routine"
    name, number = base, 2
    while name in taken:
        suffix = f"-{number}"
        name = base[: 40 - len(suffix)].strip("-") + suffix
        number += 1
    taken.add(name)
    return name


def upgrade() -> None:
    op.add_column(
        "routines", sa.Column("title", sa.String(length=120), server_default="", nullable=False)
    )
    op.add_column(
        "routines", sa.Column("description", sa.Text(), server_default="", nullable=False)
    )
    op.add_column("routines", sa.Column("sha", sa.String(length=64), nullable=True))
    op.add_column("routines", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "routines",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, space_id, name FROM routines ORDER BY created_at, name")
    ).all()
    # A unique temporary name first: a new name must never collide with an old one.
    connection.execute(sa.text("UPDATE routines SET title = name, name = id::text"))
    taken: dict[object, set[str]] = {}
    for routine_id, space_id, name in rows:
        connection.execute(
            sa.text("UPDATE routines SET name = :name WHERE id = :id"),
            {"name": _file_name(name, taken.setdefault(space_id, set())), "id": routine_id},
        )
    for column in ("title", "description", "warnings"):
        op.alter_column("routines", column, server_default=None)


def downgrade() -> None:
    # Back to a name that is the title, unique in its space.
    connection = op.get_bind()
    taken: dict[object, set[str]] = {}
    rows = connection.execute(
        sa.text("SELECT id, space_id, name, title FROM routines ORDER BY created_at, name")
    ).all()
    connection.execute(sa.text("UPDATE routines SET name = id::text"))
    for routine_id, space_id, name, title in rows:
        used = taken.setdefault(space_id, set())
        base = title or name
        chosen, number = base, 1
        # Titles need not be unique: the file name, then a number, within 120 characters.
        while chosen in used:
            suffix = f" ({name})" if number == 1 else f" ({name} {number})"
            chosen = base[: 120 - len(suffix)].rstrip() + suffix
            number += 1
        used.add(chosen)
        connection.execute(
            sa.text("UPDATE routines SET name = :name WHERE id = :id"),
            {"name": chosen, "id": routine_id},
        )
    op.drop_column("routines", "warnings")
    op.drop_column("routines", "error")
    op.drop_column("routines", "sha")
    op.drop_column("routines", "description")
    op.drop_column("routines", "title")
