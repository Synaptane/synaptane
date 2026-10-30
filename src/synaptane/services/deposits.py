"""Depositing into a space's inbox and reading it (docs/concepts.md).

A deposit is a commit that adds `.synaptane/inbox/<item>.md` and its attachments, pushed right
away. Only contributors deposit.

Each deposit may trigger a paid tidy run: an account deposits at most MAX_DEPOSITS_PER_HOUR
notes per hour, across all spaces and all interfaces (web, REST, MCP), counted in the audit
log (deposits and notes moved to another space).
"""

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, require
from synaptane.core.errors import Label, SynaptaneError, invalid, not_found, too_many
from synaptane.core.secret_scan import added_findings, refuse_secrets, scan_file, scan_text
from synaptane.git.repo import GitError, Repository, safe_path
from synaptane.services import Actor, audit, changes, jobs, verdicts, webhooks
from synaptane.services.content import MAX_UPLOAD, writable
from synaptane.services.inbox import (
    INBOX_DIR,
    InboxItem,
    attachments_dir,
    front_matter,
    note_item,
    parse_front_matter,
)
from synaptane.services.workspace import Workspace, author_identity, trailers
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space

MAX_BODY = 200_000
MAX_ATTACHMENTS = 10
MAX_DEPOSITS_PER_HOUR = 120  # per account: beyond that, a bulk import would run up the AI bill
DEPOSIT_ACTIONS = ("inbox.deposited", "inbox.moved")


def check_rate(session: Session, actor: Actor) -> None:
    """Refuses the deposit of an account that already deposited MAX_DEPOSITS_PER_HOUR notes
    within the hour (sliding window)."""
    recent = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.actor_id == actor.user.id,
            AuditEvent.action.in_(DEPOSIT_ACTIONS),
            AuditEvent.at >= datetime.now(UTC) - timedelta(hours=1),
        )
    )
    if (recent or 0) >= MAX_DEPOSITS_PER_HOUR:
        raise too_many(
            "Too many drops: at most {limit} notes per hour per account. Try again in a few "
            "minutes, or group the information into fewer notes.",
            limit=MAX_DEPOSITS_PER_HOUR,
        )


def deposit(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    *,
    title: str,
    body: str,
    source: str,
    origin: str = "",
    hint: str = "",
    attachments: list[tuple[str, bytes]] | None = None,
    allow_secrets: bool = False,
) -> str:
    """Deposits a note into the inbox; returns the path of the created item.

    A likely secret (text or attachment) is refused before any commit, unless `allow_secrets`
    (confirmed by the person)."""
    require(actor.access, Action.DEPOSIT_SPACE, org_id=space.org_id, space_id=space.id)
    check_rate(session, actor)
    title = names.display_name(title, field="Title")
    if not body.strip() and not attachments:
        raise invalid("Nothing to drop: add some text or an attachment.")
    _check_hint(hint, origin)
    if len(body) > MAX_BODY:
        raise invalid("Text too long (200,000 characters maximum).")
    attachments = attachments or []
    if len(attachments) > MAX_ATTACHMENTS:
        raise invalid("10 attachments maximum.")
    cleaned: list[tuple[str, bytes]] = []
    for name, data in attachments:
        clean = PurePosixPath(name.replace("\\", "/")).name.strip()
        if not clean or clean.startswith(".") or clean in {c for c, _ in cleaned}:
            raise invalid("Invalid or duplicate attachment name.")
        if len(data) > MAX_UPLOAD:
            raise invalid("Attachment too large (25 MB maximum).")
        cleaned.append((clean, data))
    if not allow_secrets:
        findings = scan_text(f"{title}\n{origin}\n{hint}", Label("title, origin or hint"))
        findings += scan_text(body, Label("text"))
        for name, data in cleaned:
            findings += scan_file(name, data)
        refuse_secrets(findings)
    author = author_identity(actor.user, ws.committer)
    item_path, item = note_item(
        author,
        source,
        title,
        body,
        hint,
        [n for n, _ in cleaned],
        datetime.now(UTC),
        via=actor.via,
        origin=origin,
    )
    for name, _ in cleaned:  # names checked before the lock
        try:
            safe_path(f"{attachments_dir(item_path)}/{name}")
        except GitError:
            raise invalid("Invalid or duplicate attachment name.") from None
    with writable(session, database, actor, ws, space, Action.DEPOSIT_SPACE) as repo:
        item_path = free_item_path(repo, item_path)
        folder = attachments_dir(item_path)
        files: dict[str, bytes | None] = {f"{folder}/{name}": data for name, data in cleaned}
        files[item_path] = item
        repo.commit_changes(files, author, f"Inbox: {title}" + trailers(source, actor.via))
    jobs.enqueue(session, "sync", space.id)
    tidy_soon(session, space)
    audit(
        session, actor, "inbox.deposited", space=space, path=item_path, source=source, via=actor.via
    )
    webhooks.emit(
        session,
        space,
        "inbox.item_added",
        {
            "path": item_path,
            "title": title,
            "source": source,
            "via": actor.via,
            "origin": origin.strip(),
            "by": actor.user.username,
        },
    )
    return item_path


def free_item_path(repo: Repository, path: str) -> str:
    """`path`, or the same name with another random suffix if it is already taken (note or
    attachments directory): to call under the space lock, just before the commit.

    The name (second, title, 4 hexadecimal characters) can repeat during a bulk import of
    notes with the same title: without this check, the second would overwrite the first."""
    stem = path.removesuffix(".md").rsplit("-", 1)[0]
    while repo.exists_at(path) or repo.exists_at(attachments_dir(path)):
        path = f"{stem}-{secrets.token_hex(2)}.md"
    return path


def tidy_soon(session: Session, space: Space) -> None:
    """Tidy run soon after a deposit, if the space is set that way (`tidy.schedule_soon`)."""
    from synaptane.services import tidy  # tidy reads the inbox through this module

    tidy.schedule_soon(session, space)


def list_items(actor: Actor, ws: Workspace, space: Space) -> list[InboxItem]:
    """Pending items, oldest first."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    repo = ws.local_repository(space)
    paths = [
        blob.path
        for blob in repo.files(INBOX_DIR)
        if blob.path.endswith(".md")
        and blob.path.count("/") == 2  # neither attachments nor technical files
    ]
    notes = repo.read_many(paths)  # a single git call for all the notes
    return sorted((_parse_item(p, data) for p, data in notes.items()), key=lambda i: i.path)


MAX_HINT = 500  # the hint goes as is into the agent's first message


def _check_hint(hint: str, origin: str) -> None:
    if len(hint) > MAX_HINT:
        raise invalid(
            "Filing suggestion too long (500 characters maximum): put the content in the text."
        )
    if len(origin) > 2000:
        raise invalid("Origin too long (2,000 characters maximum).")


def _without_title(body: str, title: str) -> str:
    """Body without the leading "# Title" that repeats the title (notes from before 1.0)."""
    first, _, rest = body.lstrip().partition("\n")
    if first.startswith("# ") and first[2:].strip() == title.strip():
        return rest.lstrip("\n")
    return body


def _parse_item(path: str, data: bytes) -> InboxItem:
    fields, body = parse_front_matter(data.decode("utf-8", "replace"))
    by = fields.get("deposited_by", "")
    title = str(fields.get("title") or PurePosixPath(path).stem)
    body = _without_title(body, title)
    return InboxItem(
        path=path,
        type=str(fields.get("type", "note")),
        title=title,
        deposited_by=", ".join(map(str, by)) if isinstance(by, list) else str(by),
        deposited_at=str(fields.get("deposited_at", "")),
        source=str(fields.get("source", "")),
        status=str(fields.get("status", "pending")),
        reason=str(fields.get("reason", "")),
        hint=str(fields.get("hint", "")),
        via=str(fields.get("via", "")),
        origin=str(fields.get("origin", "")),
        attachments=_strings(fields.get("attachments")),
        body=body,
    )


def inbox(session: Session, actor: Actor, ws: Workspace, space: Space) -> list[InboxItem]:
    """Everything waiting for the tidy run: deposited notes, then changed pages (Git)."""

    notes = list_items(actor, ws, space)
    return notes + [item for item, _ in changes.pending(session, ws, space)]


def describe_item(
    session: Session, actor: Actor, ws: Workspace, space: Space, name: str
) -> InboxItem:
    """One item, note or change (body: text of the note, or commits and diff)."""

    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    if name.startswith("change-"):
        for item, change in changes.pending(session, ws, space):
            if item.name == name:
                item.body = changes.describe(ws.local_repository(space), space, change)
                return item
        raise not_found("Item not found in the inbox.")
    return read_item(actor, ws, space, name)


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def read_item(actor: Actor, ws: Workspace, space: Space, name: str) -> InboxItem:
    """A note, read directly by its name (without rereading the whole inbox)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    path = f"{INBOX_DIR}/{name}"
    if "/" in name or not name.endswith(".md") or name.startswith("."):
        raise not_found("Item not found in the inbox.")
    data = ws.local_repository(space).blob(path)
    if data is None:
        raise not_found("Item not found in the inbox.")
    return _parse_item(path, data)


def read_attachment(actor: Actor, ws: Workspace, space: Space, item: str, name: str) -> bytes:
    found = read_item(actor, ws, space, item)
    if name not in found.attachments:
        raise not_found("Attachment not found.")
    data = ws.local_repository(space).blob(f"{attachments_dir(found.path)}/{name}")
    if data is None:
        raise not_found("Attachment not found.")
    return data


# --- Managing notes (contributors) ------------------------------------------------------------


def _note(actor: Actor, ws: Workspace, space: Space, name: str) -> InboxItem:
    require(actor.access, Action.DEPOSIT_SPACE, org_id=space.org_id, space_id=space.id)
    item = read_item(actor, ws, space, name)
    if item.type != "note":
        raise invalid("Only dropped notes can be edited this way.")
    return item


def update_item(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    name: str,
    *,
    title: str,
    body: str,
    hint: str = "",
    origin: str = "",
    allow_secrets: bool = False,
) -> None:
    """Corrects a note. If the tidy run had left it, it becomes pending again."""
    item = _note(actor, ws, space, name)
    title = names.display_name(title, field="Title")
    _check_hint(hint, origin)
    if len(body) > MAX_BODY or len(origin) > 2000:
        raise invalid("Text too long (200,000 characters maximum).")
    repo = ws.local_repository(space)
    fields, old_body = parse_front_matter((repo.blob(item.path) or b"").decode("utf-8", "replace"))
    if not allow_secrets:
        findings = added_findings(Label("text"), body.encode(), old_body.encode())
        findings += scan_text(f"{title}\n{origin}\n{hint}", Label("title, origin or hint"))
        refuse_secrets(findings)
    fields["title"] = title
    for key, value in (("hint", hint.strip()), ("origin", origin.strip())):
        if value:
            fields[key] = value
        else:
            fields.pop(key, None)
    for key in ("status", "reason", "left_at", "tidy_attempts"):
        fields.pop(key, None)
    data = (front_matter(fields) + "\n" + body.strip() + "\n").encode()
    author = author_identity(actor.user, ws.committer)
    with writable(session, database, actor, ws, space, Action.DEPOSIT_SPACE) as locked:
        locked.commit_changes({item.path: data}, author, f"Inbox: edit “{title}”")
    jobs.enqueue(session, "sync", space.id)
    tidy_soon(session, space)  # a note left then completed is picked up without waiting
    audit(session, actor, "inbox.updated", space=space, path=item.path)


def delete_item(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    name: str,
    *,
    not_a_problem: str | None = None,
) -> InboxItem:
    """Removes a note and its attachments (they stay in the Git history). For a problem an
    agent routine reported, what became of it is remembered (`services.verdicts`), so that
    the routine does not report it again: `not_a_problem`, the reason why it is not one."""
    item = _note(actor, ws, space, name)
    files: dict[str, bytes | None] = {item.path: None}
    for attachment in item.attachments:
        files[f"{attachments_dir(item.path)}/{attachment}"] = None
    author = author_identity(actor.user, ws.committer)
    with writable(session, database, actor, ws, space, Action.DEPOSIT_SPACE) as repo:
        repo.commit_changes(files, author, f"Inbox: delete “{item.title}”")
    kind = "deleted" if not_a_problem is None else "dismissed"
    verdicts.record(session, ws, space, item, kind, not_a_problem or "", actor.user.id)
    jobs.enqueue(session, "sync", space.id)
    audit(session, actor, "inbox.deleted", space=space, path=item.path)
    return item


def move_item(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    name: str,
    target: Space,
) -> str:
    """Moves a note (and its attachments) to the inbox of another space.

    It is first written to the target space, then removed from the original space: a failure
    in between leaves a duplicate, never a loss."""
    if target.id == space.id:
        raise invalid("Choose another space.")
    require(actor.access, Action.DEPOSIT_SPACE, org_id=target.org_id, space_id=target.id)
    check_rate(session, actor)
    item = _note(actor, ws, space, name)
    repo = ws.local_repository(space)
    fields, body = parse_front_matter((repo.blob(item.path) or b"").decode("utf-8", "replace"))
    fields["moved_from"] = space.address
    for key in ("status", "reason", "left_at", "tidy_attempts"):
        fields.pop(key, None)
    files: dict[str, bytes | None] = {item.path: (front_matter(fields) + "\n" + body).encode()}
    for attachment in item.attachments:
        path = f"{attachments_dir(item.path)}/{attachment}"
        files[path] = repo.blob(path) or b""
    author = author_identity(actor.user, ws.committer)
    address = target.address
    with writable(session, database, actor, ws, target, Action.DEPOSIT_SPACE) as destination:
        placed = free_item_path(destination, item.path)  # name already taken in the target?
        moved: dict[str, bytes | None] = {placed: files[item.path]}
        for attachment in item.attachments:
            moved[f"{attachments_dir(placed)}/{attachment}"] = files[
                f"{attachments_dir(item.path)}/{attachment}"
            ]
        destination.commit_changes(moved, author, f"Inbox: “{item.title}” from another space")
    jobs.enqueue(session, "sync", target.id)
    tidy_soon(session, target)
    with writable(session, database, actor, ws, space, Action.DEPOSIT_SPACE) as origin:
        origin.commit_changes(
            dict.fromkeys(files), author, f"Inbox: “{item.title}” moved to {address}"
        )
    jobs.enqueue(session, "sync", space.id)
    audit(session, actor, "inbox.moved", space=space, path=item.path, target=address)
    webhooks.emit(
        session,
        target,
        "inbox.item_added",
        {
            "path": placed,
            "title": item.title,
            "source": item.source,
            "via": item.via,
            "origin": item.origin,
            "by": actor.user.username,
        },
    )
    return placed


def dismiss_change(
    session: Session, actor: Actor, ws: Workspace, space: Space, name: str
) -> InboxItem:
    """A changed page that needs nothing from the tidy run: seen, until its next change.

    The watermark moves forward as soon as nothing is waiting: without a tidy run, this is the
    only way to empty the inbox, otherwise the history to reread would keep growing."""

    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    for item, change in changes.pending(session, ws, space):
        if item.name == name:
            summary = f"Seen by {actor.user.display_name}"  # kept in the decision, not shown
            # Item left with no change since: decision set on the current head.
            until = change.last if change.commits else ws.local_repository(space).head()
            assert until is not None
            changes.record(session, space, {change.path: ("done", summary, change)}, until)
            session.flush()
            changes.compact(session, ws, space)
            audit(session, actor, "inbox.change_dismissed", space=space, path=change.path)
            return item
    raise not_found("Item not found in the inbox.")


def matching(items: list[InboxItem], query: str = "", kind: str = "") -> list[InboxItem]:
    """Inbox filter: text (title, content, authors, hint, origin; ignoring accents) and type
    (`note`, `change` or `left`: left by the tidy run)."""
    from synaptane.services.search import fold, stem

    words = [stem(word) for word in fold(query).split()]  # like the wiki search
    found = []
    for item in items:
        if kind == "left" and item.status != "left":
            continue
        if kind in ("note", "change") and item.type != kind:
            continue
        haystack = fold(
            " ".join(
                (item.title, item.body, item.deposited_by, item.hint, item.origin, item.reason)
            )
        )
        if all(word in haystack for word in words):
            found.append(item)
    return found


def counts(items: list[InboxItem]) -> dict[str, int]:
    """Number of items per type, for the inbox header."""
    return {
        "note": sum(1 for i in items if i.type == "note"),
        "change": sum(1 for i in items if i.type == "change"),
        "left": sum(1 for i in items if i.status == "left"),
        "total": len(items),
    }


def pending_notes(
    actor: Actor, ws: Workspace, spaces: list[Space], query: str, limit: int = 10
) -> list[tuple[Space, InboxItem]]:
    """Notes waiting for the tidy run that contain all the words of `query` (accents ignored),
    in the readable spaces: what was just deposited is found right away."""
    from synaptane.services.search import STOPWORDS, fold

    words = [w for w in fold(query).split() if len(w) > 1 and w not in STOPWORDS]
    if not words:
        return []
    found: list[tuple[Space, InboxItem]] = []
    for space in spaces:
        try:
            notes = list_items(actor, ws, space)
        except SynaptaneError:
            continue  # space not synchronized yet
        found += [(space, note) for note in matching(notes, " ".join(words))]
    found.sort(key=lambda pair: pair[1].deposited_at, reverse=True)
    return found[:limit]
