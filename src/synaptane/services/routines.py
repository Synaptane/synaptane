"""Agent routines of a space: provided templates, settings, rights and schedule.

A routine is a set of instructions, a schedule (daily, weekly or monthly, at a local time of
the space), a scope (folders of the wiki, or the whole space) and outputs: a report page
rewritten at each run, items deposited in the inbox, an email, a `routine.done` webhook. The
agent reads the wiki; it writes nowhere but in its outputs, and in the folders assigned to it
if it has any (`writes`: pages it keeps itself, `services.routine_writes`). Actual fixes go
through the inbox and the nightly tidy-up, with their safeguards (`services.routine_agent`).

Each routine is a file of the space's repository, `.synaptane/routines/<name>.md`
(`services.routine_files`: format, reading, writing); the database keeps a copy of its
settings and the state of its runs.

The nightly tidy-up stays separate (`services.tidy`): it is not a routine.

Rights: the organization's administrators create, edit, run and delete (from Synaptane: a
commit in their name); the space's contributors see the routines and their log. Whoever can
push to the repository can change them too, like the tidy-up rules.

Scheduling: like the tidy-up, the worker checks every minute what is due (`schedule`). A
routine due receives a request (`Routine.requested`) and a single `routine` task per space
runs them all, one after the other (`jobs` merges the repeated requests of a space into one
task). An invalid file is never due.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, invalid, not_found
from synaptane.services import Actor, audit, budget, jobs
from synaptane.services.routine_files import (
    MAX_ROUTINES,
    Config,
    apply,
    check_name,
    file_path,
    name_for,
    save,
)
from synaptane.services.routine_files import Fields as Fields  # interfaces: `routines.Fields`
from synaptane.services.tidy import parse_time, zone
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Routine, RoutineRun, Space

ROUTINE = "routine"  # kind of task (the worker's slow lane, with the tidy-up)


@dataclass(frozen=True)
class Text:
    """What a routine template says, in one language."""

    instructions: str
    output_page: str = ""
    writes: tuple[str, ...] = ()  # folders it keeps (`writes`), named in the language


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
    memory: bool = False  # keeps notes from one run to the next (`services.routine_memory`)

    def text_for(self, locale: str) -> Text:
        """Instructions and report page in a language; English for any language missing."""
        return self.texts.get(locale) or self.texts["en"]

    @property
    def file_name(self) -> str:
        """Name of the routine's file, the same in every language (`consistency-check`), so
        that a team finds the template's routine under one name whoever enabled it."""
        return name_for(self.name)


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
a problem already deposited by this routine and not filed yet.

Memory (given as input, if the routine keeps one): do not report again, as new, a problem
already reported at an earlier run whose pages have not changed since; say in one line that it
is still there. Before finish, rewrite the memory with remember: the problems reported (date,
pages, item deposited or not), the doubts to check again at the next run, and, in a quiet week,
the old pages already reread, so as to reread others next time. Drop what has been fixed."""),
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
rapport), ni pour un problème déjà déposé par cette routine et pas encore rangé.

Mémoire (donnée en entrée, si la routine en garde une) : ne présente pas comme nouveau un
problème déjà signalé lors d'un passage précédent, si ses pages n'ont pas changé depuis ; dis
en une ligne qu'il est toujours là. Avant finish, réécris la mémoire avec
remember : les problèmes signalés (date, pages, élément déposé ou non), les doutes à revérifier
au prochain passage et, une semaine calme, les pages anciennes déjà relues, pour en relire
d'autres la fois suivante. Retire ce qui a été corrigé."""),
            "rapports/coherence.md",
        ),
    },
    frequency="weekly",
    weekday=0,
    at="06:00",
    output_inbox=True,
    memory=True,
)

DIGEST = Template(
    key="digest",
    name="Weekly digest",
    description="Every Friday, what the team learned and decided during the week, from the "
    "wiki history and the tidy-up. A page, and an email to the space's members.",
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

ARCHIVE = Template(
    key="archive",
    name="Digest archive",
    description="Every Friday, the summary of the week as a page of its own in a folder the "
    "routine keeps, listed in that folder's index: the weeks stay readable one by one.",
    texts={
        "en": Text(
            flow("""\
Write the summary of the week, from the activity given as input (Git history of the wiki,
tidy-up runs: items filed and left), as a new page of the digests/ folder named after the
ISO week given as input: digests/<year>-W<week>.md (for example digests/2026-W46.md).
- A level-1 title with the week, then three to five key points, one line each.
- Then, in short sections: pages created, pages heavily changed (say what changed, by
  reading the pages), decisions made, open questions (items left in the inbox).
- Put a link to every page mentioned, relative to the new page (for example
  [Proxmox](../servers/proxmox.md)).
- Stay factual and brief: a reader must go through it all in two minutes. Invent nothing; if
  the week was quiet, say so in one sentence.
Then add the new page at the top of the list of digests/README.md (create it with a title if
it does not exist yet), without changing the rest. Leave the digests of the previous weeks as
they are. Call finish with a one-sentence wrap-up."""),
            writes=("digests",),
        ),
        "fr": Text(
            flow("""\
Rédige le résumé de la semaine, à partir de l'activité donnée en entrée (historique Git du
wiki, passages du rangement : éléments rangés et laissés), dans une nouvelle page du dossier
resumes/ nommée d'après la semaine ISO donnée en entrée : resumes/<année>-S<semaine>.md (par
exemple resumes/2026-S46.md).
- Un titre de niveau 1 avec la semaine, puis trois à cinq points essentiels, en une ligne
  chacun.
- Puis, en sections courtes : pages créées, pages très modifiées (dis ce qui a changé, en
  lisant les pages), décisions prises, questions en suspens (éléments laissés dans l'inbox).
- Mets un lien vers chaque page citée, relatif à la nouvelle page (par exemple
  [Proxmox](../serveurs/proxmox.md)).
- Reste factuel et bref : un lecteur doit tout parcourir en deux minutes. N'invente rien ; si
  la semaine a été calme, dis-le en une phrase.
Ajoute ensuite la nouvelle page en tête de la liste de resumes/README.md (crée-la avec un
titre si elle n'existe pas encore), sans changer le reste. Laisse les résumés des semaines
précédentes tels quels. Appelle finish avec une phrase de synthèse."""),
            writes=("resumes",),
        ),
    },
    frequency="weekly",
    weekday=4,
    at="16:30",
)

TEMPLATES: dict[str, Template] = {t.key: t for t in (COHERENCE, DIGEST, ARCHIVE)}


def from_template(key: str, locale: str) -> Config:
    """A template's settings, its title, instructions, report page and folders in a
    language."""
    model = TEMPLATES.get(key)
    if model is None:
        raise invalid("Unknown routine template.")
    from synaptane.web.i18n import translator

    text = model.text_for(locale)
    config = Config(template=model.key)
    apply(
        config,
        Fields(
            title=translator(locale)(model.name),
            instructions=text.instructions,
            frequency=model.frequency,
            weekday=model.weekday,
            monthday=model.monthday,
            at=model.at,
            output_page=text.output_page,
            writes=list(text.writes),
            output_inbox=model.output_inbox,
            output_email=model.output_email,
            output_webhook=model.output_webhook,
            memory=model.memory,
        ),
    )
    return config


# --- Use cases ----------------------------------------------------------------------------


def list_routines(session: Session, actor: Actor, space: Space) -> list[Routine]:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    return list(
        session.scalars(
            select(Routine)
            .where(Routine.space_id == space.id)
            .order_by(Routine.title, Routine.name)
        )
    )


def get(session: Session, actor: Actor, space: Space, routine_id: UUID) -> Routine:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    routine = session.get(Routine, routine_id)
    if routine is None or routine.space_id != space.id:
        raise not_found("Routine not found.")
    return routine


def path_of(routine: Routine) -> str:
    """The routine's file in the repository."""
    return file_path(routine.name)


def create(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    fields: Fields,
    *,
    name: str = "",
    template: str = "",
    allow_secrets: bool = False,
    source: str = "web",
) -> Routine:
    """New routine, blank or from a template (its settings in the creator's language, then
    `fields`): a new file `.synaptane/routines/<name>.md`. `name` defaults to the template's
    (`consistency-check`, `weekly-digest`), or else to one made from the title."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    count = len(list(session.scalars(select(Routine.id).where(Routine.space_id == space.id))))
    if count >= MAX_ROUTINES:
        raise invalid("{n} routines at most per space.", n=MAX_ROUTINES)
    config = from_template(template, actor.user.locale) if template else Config()
    apply(config, fields)
    if not config.title or not config.instructions:
        raise invalid("Give the routine a title and instructions.")
    if name.strip():
        file_name = check_name(name)
    elif template:
        file_name = TEMPLATES[template].file_name  # whatever the language of the title
    else:
        file_name = name_for(config.title)
    routine = save(
        session,
        database,
        actor,
        ws,
        space,
        file_name,
        config,
        creating=True,
        allow_secrets=allow_secrets,
        source=source,
    )
    assert routine is not None
    audit(
        session,
        actor,
        "routine.created",
        space=space,
        routine=routine.title,
        file=path_of(routine),
        template=template,
    )
    return routine


def update(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    routine: Routine,
    fields: Fields,
    *,
    base_sha: str | None = None,
    allow_secrets: bool = False,
    source: str = "web",
) -> Routine:
    """Rewrites the routine's file with `fields` changed. `base_sha`: the file's hash when
    the form was opened (conflict if it changed since); None: no check."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    config = Config.of(routine)
    apply(config, fields)
    saved = save(
        session,
        database,
        actor,
        ws,
        space,
        routine.name,
        config,
        base_sha=base_sha,
        allow_secrets=allow_secrets,
        source=source,
    )
    assert saved is not None
    audit(session, actor, "routine.updated", space=space, routine=saved.title)
    return saved


def delete(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    routine: Routine,
    *,
    base_sha: str | None = None,
    source: str = "web",
) -> None:
    """Removes the routine's file, and its memory in the same commit. Its runs stay in the
    log (and in the month's budget); the report page stays in the wiki, like any page."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    title = routine.title
    save(session, database, actor, ws, space, routine.name, None, base_sha=base_sha, source=source)
    audit(session, actor, "routine.deleted", space=space, routine=title)


def request_run(session: Session, actor: Actor, space: Space, routine: Routine) -> None:
    """ "Run now": the worker runs it as soon as the slow lane is free."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if routine.error is not None:
        raise invalid("The routine's file is invalid: fix it before running the routine.")
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
    audit(session, actor, "routine.requested", space=space, routine=routine.title)


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
    """Local day of the run due, or None (disabled, invalid file, not the right day, already
    done, or time not reached yet). Time zone of the space, the tidy-up's one."""
    if not routine.enabled or routine.error is not None or not space.repo_url:
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
    """Next scheduled run (local time of the space), or None if disabled or invalid."""
    if not routine.enabled or routine.error is not None or not space.repo_url:
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
            .where(
                Routine.enabled.is_(True),
                Routine.error.is_(None),
                Space.repo_url.is_not(None),
            )
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
