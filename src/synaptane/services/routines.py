"""Agent routines of a space: provided templates, settings, rights and schedule.

A routine is a set of instructions, a schedule (daily, weekly or monthly, at a local time of
the space), a scope (folders of the wiki, or the whole space) and outputs: a report page
rewritten at each run, items deposited in the inbox, an e-mail, a `routine.done` webhook. The
agent reads the wiki; it writes nowhere but in its outputs. Actual fixes go through the inbox
and the nightly tidy-up, with their safeguards (`services.routine_agent`).

The nightly tidy-up stays separate (`services.tidy`): it is not a routine.

Rights: the organisation's administrators create, edit, run and delete; the space's
contributors see the routines and their log.

Scheduling: like the tidy-up, the worker checks every minute what is due (`schedule`). A
routine due receives a request (`Routine.requested`) and a single `routine` task per space
runs them all, one after the other (`jobs` merges the repeated requests of a space into one
task).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, conflict, invalid, not_found
from synaptane.services import Actor, audit, budget, jobs
from synaptane.services.content import content_path
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.tidy import parse_time, zone
from synaptane.storage.db import Database
from synaptane.storage.models import Routine, RoutineRun, Space

ROUTINE = "routine"  # kind of task (the worker's slow lane, with the tidy-up)
FREQUENCIES = ("daily", "weekly", "monthly")
EMAIL_TARGETS = ("", "admins", "members")
MAX_INSTRUCTIONS = 20_000
MAX_SCOPE = 20  # folders
MAX_ROUTINES = 20  # per space: each routine costs, proliferation is paid for


@dataclass(frozen=True)
class Text:
    """What a routine template says, in one language."""

    instructions: str
    output_page: str = ""


@dataclass(frozen=True)
class Template:
    """Provided routine template: the admin enables it in one click, then adjusts it.

    `name` and `description` are interface texts, translated by the web catalog; the
    instructions and the report page exist in each language of `texts` ("en" always)."""

    key: str
    name: str
    description: str
    texts: dict[str, Text]
    frequency: str
    at: str
    weekday: int | None = None
    monthday: int | None = None
    output_inbox: bool = False
    output_email: str = ""
    output_webhook: bool = False

    def text_for(self, locale: str) -> Text:
        """Instructions and report page in a language; English for any language missing."""
        return self.texts.get(locale) or self.texts["en"]


def flow(text: str) -> str:
    """Text written at 100 columns in the code → paragraphs and bullets of one line each, as
    they are read and edited in the form. An indented line continues the preceding bullet; a
    plain line continues the paragraph, except after a bullet."""
    lines: list[str] = []
    bullet = False
    for line in text.splitlines():
        if not line.strip():
            lines.append("")
            bullet = False
        elif line.startswith("- "):
            lines.append(line)
            bullet = True
        elif lines and lines[-1] and (line.startswith("  ") or not bullet):
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
            bullet = False
    return "\n".join(lines)


COHERENCE = Template(
    key="coherence",
    name="Consistency check",
    description="Every week, reviews the changed pages and the pages linked to them: "
    "contradictions, outdated information, duplicates. A report, and one inbox item per fix to "
    "make.",
    texts={
        "en": Text(
            flow("""\
Read the pages to review first (list given as input: pages changed since the last run, then
linked pages), and read the pages related to them when useful. Look for:
- contradictions between pages: figures, dates, owners, addresses or procedures that differ
  for the same thing;
- information that is probably outdated: past dates presented as upcoming, "in progress",
  "soon", "planned for", outdated versions of software or tools;
- duplicate topics: two pages that cover the same thing and would gain from being merged.
Also rely on the wiki health check given as input (broken links, orphan pages, old pages,
pages due for review). A page due for review (verification expired) is read first: say in the
report what still seems right or outdated, and name its owner; it is up to them to mark it
as verified.

Report (write_report): a level-1 title, today's date, then one section per type of problem;
for each problem, the pages concerned (as links) and a short excerpt. End with the wiki
health points most worth fixing. If all is well, say so in one sentence.

For each problem that requires a fix in the wiki, deposit an item in the inbox
(add_to_inbox): what is wrong, the pages and excerpts concerned, and the fix suggested. The
nightly tidy-up will process it. No item for a mere doubt (mention it in the report), nor for
a problem already deposited by this routine and not filed yet."""),
            "reports/consistency.md",
        ),
        "fr": Text(
            flow("""\
Relis les pages à relire en priorité (liste donnée en entrée : pages modifiées depuis le dernier
passage, puis pages liées), et lis les pages qui s'y rattachent quand c'est utile. Repère :
- les contradictions entre pages : chiffres, dates, responsables, adresses ou procédures
  différents pour la même chose ;
- les informations probablement périmées : dates passées présentées comme à venir, « en
  cours », « bientôt », « prévu pour », versions de logiciels ou d'outils dépassées ;
- les doublons de sujet : deux pages qui traitent de la même chose et gagneraient à être
  fusionnées.
Appuie-toi aussi sur le contrôle de santé du wiki donné en entrée (liens cassés, pages
orphelines, pages anciennes, pages à revoir). Une page à revoir (vérification échue) se relit
en priorité : dis dans le rapport ce qui semble encore juste ou dépassé, et nomme son
propriétaire ; c'est à lui de la marquer comme vérifiée.

Rapport (write_report) : un titre de niveau 1, la date du jour, puis une section par type de
problème ; pour chaque problème, les pages concernées (en liens) et un court extrait. Termine
par les points de santé du wiki les plus utiles à corriger. Si tout va bien, dis-le en une
phrase.

Pour chaque problème qui demande une correction dans le wiki, dépose un élément dans l'inbox
(add_to_inbox) : ce qui ne va pas, les pages et extraits concernés, et la correction proposée.
Le rangement de la nuit le traitera. Pas d'élément pour un simple doute (mentionne-le dans le
rapport), ni pour un problème déjà déposé par cette routine et pas encore rangé."""),
            "rapports/coherence.md",
        ),
    },
    frequency="weekly",
    weekday=0,
    at="06:00",
    output_inbox=True,
)

DIGEST = Template(
    key="digest",
    name="Weekly digest",
    description="Every Friday, what the team learned and decided during the week, from the "
    "wiki history and tidying. A page, and an e-mail to the space's members.",
    texts={
        "en": Text(
            flow("""\
Write the summary of the week: what the team learned and decided, from the activity given as
input (Git history of the wiki, tidy-up runs: items filed and left).
- Start with three to five key points, one line each.
- Then, in short sections: pages created, pages heavily changed (say what changed, by
  reading the pages), decisions made, open questions (items left in the inbox).
- Put a link to every page mentioned.
- Stay factual and brief: a reader must go through it all in two minutes. Invent nothing; if
  the week was quiet, say so in one sentence.
Write the summary with write_report, then call finish with a one-sentence wrap-up."""),
            "reports/weekly-digest.md",
        ),
        "fr": Text(
            flow("""\
Rédige le résumé de la semaine : ce que l'équipe a appris et décidé, à partir de l'activité
donnée en entrée (historique Git du wiki, passages du rangement : éléments rangés et laissés).
- Commence par trois à cinq points essentiels, en une ligne chacun.
- Puis, en sections courtes : pages créées, pages très modifiées (dis ce qui a changé, en
  lisant les pages), décisions prises, questions en suspens (éléments laissés dans l'inbox).
- Mets un lien vers chaque page citée.
- Reste factuel et bref : un lecteur doit tout parcourir en deux minutes. N'invente rien ; si
  la semaine a été calme, dis-le en une phrase.
Écris le résumé avec write_report, puis appelle finish avec une phrase de synthèse."""),
            "rapports/semaine.md",
        ),
    },
    frequency="weekly",
    weekday=4,
    at="16:00",
    output_email="members",
)

TEMPLATES: dict[str, Template] = {t.key: t for t in (COHERENCE, DIGEST)}


@dataclass
class Fields:
    """Settings of a routine; None: unchanged (partial update)."""

    name: str | None = None
    instructions: str | None = None
    frequency: str | None = None
    weekday: int | None = None
    monthday: int | None = None
    at: str | None = None
    scope: list[str] | None = None
    output_page: str | None = None
    output_inbox: bool | None = None
    output_email: str | None = None
    output_webhook: bool | None = None
    enabled: bool | None = None


# --- Validation ---------------------------------------------------------------------------


def clean_folder(folder: str) -> str:
    """Folder of the scope, relative to content/ ("infra/servers")."""
    full = content_path(folder.removeprefix("content/"))
    if full == CONTENT_DIR:
        return ""
    return full[len(CONTENT_DIR) + 1 :]


def clean_scope(folders: list[str]) -> list[str]:
    cleaned = [f for f in dict.fromkeys(clean_folder(f) for f in folders if f.strip()) if f]
    if len(cleaned) > MAX_SCOPE:
        raise invalid("Scope: {n} folders at most.", n=MAX_SCOPE)
    return cleaned


def clean_page(page: str) -> str:
    """Output page, relative to content/; "": no page."""
    page = page.strip().removeprefix("content/").strip("/")
    if not page:
        return ""
    if not page.lower().endswith(".md"):
        page += ".md"
    full = content_path(page)
    if full == CONTENT_DIR:
        return ""
    return full[len(CONTENT_DIR) + 1 :]


def _apply(routine: Routine, fields: Fields) -> None:
    if fields.name is not None:
        routine.name = names.display_name(fields.name, field="Name")
    if fields.instructions is not None:
        text = fields.instructions.strip()
        if not text:
            raise invalid("Instructions: say what the routine must do.")
        if len(text) > MAX_INSTRUCTIONS:
            raise invalid("Instructions too long (20,000 characters at most).")
        routine.instructions = text
    if fields.frequency is not None:
        if fields.frequency not in FREQUENCIES:
            raise invalid("Unknown frequency (daily, weekly or monthly).")
        routine.frequency = fields.frequency
    if fields.weekday is not None:
        routine.weekday = fields.weekday
    if fields.monthday is not None:
        routine.monthday = fields.monthday
    if fields.at is not None:
        routine.at = parse_time(fields.at).strftime("%H:%M")
    if fields.scope is not None:
        routine.scope = clean_scope(fields.scope)
    if fields.output_page is not None:
        routine.output_page = clean_page(fields.output_page)
    if fields.output_inbox is not None:
        routine.output_inbox = fields.output_inbox
    if fields.output_email is not None:
        if fields.output_email not in EMAIL_TARGETS:
            raise invalid("Unknown e-mail recipients.")
        routine.output_email = fields.output_email
    if fields.output_webhook is not None:
        routine.output_webhook = fields.output_webhook
    if fields.enabled is not None:
        routine.enabled = fields.enabled
    # Consistency of the whole, once everything is applied.
    if routine.frequency == "weekly":
        if routine.weekday is None or not 0 <= routine.weekday <= 6:
            raise invalid("Choose the day of the week.")
        routine.monthday = None
    elif routine.frequency == "monthly":
        if routine.monthday is None or not 1 <= routine.monthday <= 28:
            raise invalid("Day of the month: from 1 to 28.")
        routine.weekday = None
    else:
        routine.weekday = routine.monthday = None
    if not (
        routine.output_page
        or routine.output_inbox
        or routine.output_email
        or routine.output_webhook
    ):
        raise invalid("Choose at least one output: page, inbox, e-mail or webhook.")


def _skip_today(routine: Routine, space: Space, now: datetime | None = None) -> None:
    """A routine created, re-enabled or rescheduled after its time of the day does not start
    right away (and costs nothing unless asked): "Run now" does that."""
    try:
        local = (now or datetime.now(UTC)).astimezone(zone(space.tidy_timezone))
        at = parse_time(routine.at)
    except SynaptaneError:
        return
    if local.time() >= at:
        routine.last_date = local.date()


# --- Use cases ----------------------------------------------------------------------------


def list_routines(session: Session, actor: Actor, space: Space) -> list[Routine]:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    return list(
        session.scalars(select(Routine).where(Routine.space_id == space.id).order_by(Routine.name))
    )


def get(session: Session, actor: Actor, space: Space, routine_id: UUID) -> Routine:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    routine = session.get(Routine, routine_id)
    if routine is None or routine.space_id != space.id:
        raise not_found("Routine not found.")
    return routine


def _unique_name(session: Session, space: Space, name: str, routine_id: UUID | None) -> None:
    other = session.scalar(
        select(Routine.id).where(Routine.space_id == space.id, Routine.name == name)
    )
    if other is not None and other != routine_id:
        raise conflict("A routine with this name already exists in the space.")


def create(
    session: Session, actor: Actor, space: Space, fields: Fields, *, template: str = ""
) -> Routine:
    """New routine, blank or from a template (its settings in the creator's language, then
    `fields`)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    count = len(list(session.scalars(select(Routine.id).where(Routine.space_id == space.id))))
    if count >= MAX_ROUTINES:
        raise invalid("{n} routines at most per space.", n=MAX_ROUTINES)
    routine = Routine(
        space_id=space.id,
        template="",
        name="",
        instructions="",
        frequency="weekly",
        weekday=0,
        at="06:00",
        scope=[],
        output_page="",
        output_inbox=False,
        output_email="",
        output_webhook=False,
        enabled=True,
        created_by=actor.user.id,
    )
    if template:
        model = TEMPLATES.get(template)
        if model is None:
            raise invalid("Unknown routine template.")
        routine.template = model.key
        text = model.text_for(actor.user.locale)
        from synaptane.web.i18n import translator

        _apply(
            routine,
            Fields(
                name=translator(actor.user.locale)(model.name),  # in the creator's language
                instructions=text.instructions,
                frequency=model.frequency,
                weekday=model.weekday,
                monthday=model.monthday,
                at=model.at,
                output_page=text.output_page,
                output_inbox=model.output_inbox,
                output_email=model.output_email,
                output_webhook=model.output_webhook,
            ),
        )
    _apply(routine, fields)
    if not routine.name or not routine.instructions:
        raise invalid("Give the routine a name and instructions.")
    _unique_name(session, space, routine.name, None)
    _skip_today(routine, space)
    session.add(routine)
    session.flush()
    audit(session, actor, "routine.created", space=space, routine=routine.name, template=template)
    return routine


def update(session: Session, actor: Actor, space: Space, routine: Routine, fields: Fields) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    before = (routine.enabled, routine.frequency, routine.weekday, routine.monthday, routine.at)
    _apply(routine, fields)
    _unique_name(session, space, routine.name, routine.id)
    after = (routine.enabled, routine.frequency, routine.weekday, routine.monthday, routine.at)
    if after != before and routine.enabled:
        _skip_today(routine, space)
    routine.updated_at = datetime.now(UTC)
    audit(session, actor, "routine.updated", space=space, routine=routine.name)


def delete(session: Session, actor: Actor, space: Space, routine: Routine) -> None:
    """Deletes the routine. Its runs stay in the log (and in the month's budget); the report
    page stays in the wiki, like any page."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    audit(session, actor, "routine.deleted", space=space, routine=routine.name)
    session.delete(routine)


def request_run(session: Session, actor: Actor, space: Space, routine: Routine) -> None:
    """ "Run now": the worker runs it as soon as the slow lane is free."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if not space.repo_url:
        raise invalid(
            "The space is not linked to any Git repository: nothing for the routine to read."
        )
    if budget.spending(session, space.org_id).exhausted:
        raise invalid(
            "Monthly AI budget reached: the routine cannot run before next month, or raise the "
            "budget (organization AI page)."
        )
    routine.requested, routine.requested_by = "manual", actor.user.id
    jobs.enqueue(session, ROUTINE, space.id)
    audit(session, actor, "routine.requested", space=space, routine=routine.name)


def runs(
    session: Session,
    actor: Actor,
    space: Space,
    routine_id: UUID | None = None,
    limit: int = 30,
) -> list[RoutineRun]:
    """Log of the runs, from the most recent to the oldest (of a routine, or of the space)."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    where = [RoutineRun.space_id == space.id]
    if routine_id is not None:
        where.append(RoutineRun.routine_id == routine_id)
    return list(
        session.scalars(
            select(RoutineRun).where(*where).order_by(RoutineRun.started_at.desc()).limit(limit)
        )
    )


def last_runs(session: Session, space: Space) -> dict[UUID, RoutineRun]:
    """Last run of each routine of the space (list of the routines)."""
    latest: dict[UUID, RoutineRun] = {}
    for run in session.scalars(
        select(RoutineRun)
        .where(RoutineRun.space_id == space.id, RoutineRun.routine_id.is_not(None))
        .order_by(RoutineRun.started_at.desc())
        .limit(200)
    ):
        assert run.routine_id is not None
        latest.setdefault(run.routine_id, run)
    return latest


# --- Schedule -----------------------------------------------------------------------------


def _matches(routine: Routine, day: date) -> bool:
    if routine.frequency == "weekly":
        return day.weekday() == routine.weekday
    if routine.frequency == "monthly":
        return day.day == routine.monthday
    return True


def due(routine: Routine, space: Space, now: datetime) -> date | None:
    """Local day of the run due, or None (disabled, not the right day, already done, or time
    not reached yet). Time zone of the space, the tidy-up's one."""
    if not routine.enabled or not space.repo_url:
        return None
    try:
        local = now.astimezone(zone(space.tidy_timezone))
        at = parse_time(routine.at)
    except SynaptaneError:
        return None
    if local.time() < at or routine.last_date == local.date():
        return None
    return local.date() if _matches(routine, local.date()) else None


def next_run(routine: Routine, space: Space, now: datetime | None = None) -> datetime | None:
    """Next scheduled run (local time of the space), or None if disabled."""
    if not routine.enabled or not space.repo_url:
        return None
    try:
        tz = zone(space.tidy_timezone)
        at = parse_time(routine.at)
    except SynaptaneError:
        return None
    local = (now or datetime.now(UTC)).astimezone(tz)
    for offset in range(0, 62):
        day = local.date() + timedelta(days=offset)
        if not _matches(routine, day):
            continue
        if offset == 0 and (local.time() >= at or routine.last_date == day):
            continue
        return datetime.combine(day, time(at.hour, at.minute), tz)
    return None


def schedule(database: Database, now: datetime | None = None) -> int:
    """Queues the runs due (one task per space). Returns their number."""
    now = now or datetime.now(UTC)
    count = 0
    with database.session() as session:
        rows = session.execute(
            select(Routine, Space)
            .join(Space, Space.id == Routine.space_id)
            .where(Routine.enabled.is_(True), Space.repo_url.is_not(None))
        ).all()
        for routine, space in rows:
            day = due(routine, space, now)
            if day is None:
                continue
            routine.last_date = day
            if routine.requested is None:  # a waiting manual request keeps its author
                routine.requested = "schedule"
            jobs.enqueue(session, ROUTINE, space.id)
            count += 1
    return count
