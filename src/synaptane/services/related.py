"""Pages related to a page: discover without searching, and fewer duplicates.

For a page, the closest pages, each with its reason:

- **same topic**: meaning, when the space has embeddings. The page's vector is the mean of
  its chunks; every other page is judged by its chunk closest to that vector. The computation
  runs in PostgreSQL (pgvector), in a single scan of the space's chunks (no ANN index: the
  column has no dimension), under a timeout. Similarity is scaled to the space (the median
  page is 0, the page itself 1): the threshold does not depend on the embedding model.
- **shared words**: rare terms in common. Each text file (and text extracted from PDFs,
  Office documents and images) has a profile: its most frequent terms, folded (accents,
  ligatures) and stemmed as in search, the title counted three times. Closeness is a BM25 of
  the page's terms in the other, relative to that of the page itself (1: same terms, same
  proportions).
- **cites this page**, **linked from this page**, **shared links**: the link index
  (`services.links`). A page that points to everything (index, report) counts little.
- **same folder**: a slight bonus, never enough on its own.

The signals are weighted then added up (score from 0 to 1); nothing is shown under
`THRESHOLD`. The page itself is excluded, as are files without text; pages it already points
to remain, marked `linked` ("already linked").

Cost and caches (all in process memory, nothing in the database):

- an **index per space and per commit**: file profiles, inverted term lists, links. Profiles
  are carried over from the previous commit by blob hash: a new commit only reads the changed
  files again. The first computation for a large space reads the whole wiki (a few seconds
  at 20,000 pages), once per process;
- the **result for each page, per commit**: a commit invalidates everything, at no cost;
- the **neighbourhood by meaning**, the most expensive (a scan of the space's chunks), kept
  for an hour per page hash: a commit elsewhere changes it little, and pages that disappeared
  are dropped at each read.
"""

import json
import logging
import math
import os
import posixpath
import re
import sys
import tempfile
import threading
import time
from array import array
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import text as sql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from synaptane.ai.base import AIError
from synaptane.ai.embeddings import Embedder, EmbeddingConfig, prefixes
from synaptane.core.access import Action, require
from synaptane.core.errors import invalid, not_found
from synaptane.git.repo import Blob, GitError, Repository
from synaptane.services import Actor, deposits, links, semantic
from synaptane.services.content import IMAGES, OFFICE, kind_of
from synaptane.services.content import MARKDOWN as MARKDOWN_SUFFIXES
from synaptane.services.extraction import read_json
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.search import STOPWORDS, fold, stem
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space

log = logging.getLogger("synaptane.related")

PREFIX = CONTENT_DIR + "/"
LIMIT = 8  # related pages shown by default (wiki panel, MCP)
MAX_LIMIT = 20
THRESHOLD = 0.2  # below this, a page is not "related": nothing rather than noise
DUPLICATE = 0.7  # very close content (meaning or words): likely duplicate
TERMS = 24  # terms kept in a file's profile
TITLE_WEIGHT = 3  # a word of the title counts as three occurrences
MAX_CHARS = 60_000  # start of the profiled text
MAX_TEXT = 2 * 1024 * 1024  # beyond this, a file is not profiled (as when rendering)
MAX_SNIFF = 256 * 1024  # file with an unknown extension: read to know whether it is text
COMMON = 0.12  # a term present in more than 12% of the files does not help tell them apart
HUB = 25  # a page pointing to more pages is an index: its links count little
K1, B = 1.2, 0.75
MEANING_TIMEOUT_MS = 4000  # scan of the space's chunks (meaning), at most
MEANING_TTL = 3600  # seconds: neighbourhood by meaning reused while the page has not changed
MEANING_RETRY = 120  # seconds: page without a vector (not indexed yet), asked again after
MEANING_CANDIDATES = 60
# Profiles saved next to the space's extracted text; rewritten after a first computation, or
# when at least SAVE_AFTER files were read again at once.
SAVED = "pages-proches.json"
SAVED_VERSION = 1
SAVE_AFTER = 200

# Signal weights; "words" weighs less when meaning is available (it overlaps with it).
WEIGHTS = {
    "meaning": 0.55,
    "words": 0.65,
    "backlink": 0.25,
    "link": 0.15,
    "shared": 0.1,
    "folder": 0.08,
}
WORDS_WITH_MEANING = 0.35

# Reason shown in the web interface for each signal: keys of the translation catalog, passed
# to `_()` by the template.
LABELS = {
    "meaning": "same topic",
    "words": "shared words",
    "backlink": "cites this page",
    "link": "linked from this page",
    "shared": "shared links",
    "folder": "same folder",
}
# The same reasons in plain English, returned as is to the REST API, the MCP and the agents.
REASONS = {
    "meaning": "same topic",
    "words": "shared words",
    "backlink": "cites this page",
    "link": "linked from this page",
    "shared": "shared links",
    "folder": "same folder",
}

FOLDER_INDEXES = {"readme.md", "index.md"}
EXTRACTED = ("pdf", "office", "image")
HTML = (".html", ".htm")

# Stop words on top of the search ones (folded: no accents): frequent everywhere, they would
# bring any pages together.
_MORE_STOPWORDS = """
pour avec dans plus tout tous toute toutes mais comme fait faire aussi etre avoir sans sous
entre chez vers depuis donc alors ainsi tres peut peu bien chaque autre autres meme cette
celui celle ceux elles nous vous leur leurs quand ici voir lien liens page pages fichier
dont sinon encore deja apres avant selon lors puis non oui car cela ceci tant trop
with from this that have has had will would your their there which about into when where
what also more only some other than then them they been were such each very can could
should must may might here just like over under after before https http www com html
title tags date
"""
STOP = frozenset(fold(w) for w in STOPWORDS) | frozenset(_MORE_STOPWORDS.split())
_WORD = re.compile(r"\w+")
_TAG = re.compile(r"<[^>\n]{0,500}>")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_H1 = re.compile(r" {0,3}#[ \t]+(.+?)[ \t#]*$")
_TITLE_FIELD = re.compile(r"title[ \t]*:[ \t]*['\"]?(.+?)['\"]?[ \t]*$")


@dataclass(frozen=True)
class Neighbour:
    """A related page. `reasons`: signals, strongest first (see LABELS)."""

    path: str  # relative to content/
    title: str
    score: float  # 0 to 1
    reasons: tuple[str, ...]
    linked: bool  # the page already points to it
    closeness: float = 0.0  # closeness of the content alone (meaning or words), 0 to 1
    close_by: str = "words"  # the signal giving `closeness`: meaning or words

    @property
    def reason(self) -> str:
        return self.reasons[0]

    @property
    def label(self) -> str:
        """The main reason in plain English (REST, MCP, agents; see REASONS)."""
        return REASONS[self.reason]

    @property
    def duplicate(self) -> bool:
        """Very close content: probably the same topic covered twice."""
        return self.closeness >= DUPLICATE


@dataclass(frozen=True)
class Pair:
    """Two pages with very close content (likely duplicate)."""

    first: str
    second: str
    closeness: float
    reason: str


# --- Profiles ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Profile:
    title: str
    terms: tuple[str, ...]
    counts: tuple[int, ...]  # occurrences (255 at most)
    length: int  # words counted (BM25)


@lru_cache(maxsize=200_000)
def _term(word: str) -> str | None:
    """Term of a word of the text (already lowercase): folded, stemmed; None for a stop word,
    a word too short, too long or without a letter (number, identifier)."""
    folded = fold(word)
    if not 3 <= len(folded) <= 40 or folded in STOP or folded.isdigit():
        return None
    if not any(c.isalpha() for c in folded):
        return None
    return sys.intern(stem(folded))


def fast_title(text: str, fallback: str) -> str:
    """Title of a Markdown page without parsing all of it: first "# title" (outside a code
    block), otherwise the front matter's `title:`, otherwise `fallback`."""
    lines = text[:20_000].splitlines()
    fence = ""
    field_title = ""
    start = 0
    if lines and lines[0].strip() == "---":
        for number, line in enumerate(lines[1:200], start=1):
            if line.strip() in ("---", "..."):
                start = number + 1
                break
            if found := _TITLE_FIELD.match(line):
                field_title = found.group(1).strip()
    for line in lines[start : start + 200]:
        if fence:
            if line.strip().startswith(fence):
                fence = ""
            continue
        if opening := _FENCE.match(line):
            fence = opening.group(1)
            continue
        if heading := _H1.match(line):
            return heading.group(1).strip() or fallback
    return field_title or fallback


@lru_cache(maxsize=300_000)
def _terms(token: str) -> tuple[str, ...]:
    """Terms of a piece of text without spaces ("(proxmox)," or "l'installation")."""
    return tuple(t for word in _WORD.findall(token) if (t := _term(word)) is not None)


def profile_of(text: str, title: str) -> _Profile:
    """Profile of a text: its TERMS most frequent terms (title counted three times).

    The text is split on whitespace (in C), and only the distinct pieces go through folding
    and stemming, kept in a cache: a few hundred per page."""
    counts = Counter(text[:MAX_CHARS].lower().split())
    for token in title.lower().split():
        counts[token] += TITLE_WEIGHT
    terms: Counter[str] = Counter()
    length = 0
    for token, number in counts.items():
        for term in _terms(token):
            terms[term] += number
            length += number
    top = sorted(terms.items(), key=lambda item: (-item[1], item[0]))[:TERMS]
    return _Profile(
        title, tuple(t for t, _ in top), tuple(min(n, 255) for _, n in top), max(length, 1)
    )


def _stem_name(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def _kind(path: str) -> str:
    """Like `content.kind_of` without content, without pathlib (called for every file)."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGES:
        return "image"
    if suffix in OFFICE:
        return "office"
    return "other"


# --- Index of a space -------------------------------------------------------------------------


@dataclass
class _Index:
    head: str
    files: frozenset[str]  # all the files of content/ (relative paths)
    profiles: dict[str, tuple[str, _Profile | None]]  # path → (signature, profile)
    order: list[str]  # profiled files, in the order of the inverted lists
    position: dict[str, int]
    postings: dict[str, "array[int]"]  # term → position << 8 | occurrences
    lengths: list[int]  # length of each profile, in order (BM25)
    average: float  # average profile length
    out: dict[str, set[str]]  # outgoing links of the Markdown pages
    into: dict[str, set[str]]  # pages pointing to each file

    def profile(self, path: str) -> _Profile | None:
        entry = self.profiles.get(path)
        return entry[1] if entry else None


_LOCK = threading.Lock()
_BUILDING: dict[str, threading.Lock] = {}  # one index computation at a time per space
_INDEXES: OrderedDict[str, _Index] = OrderedDict()  # clone → index of the last commit seen
_INDEXES_MAX = 6
_RESULTS: OrderedDict[tuple[str, str, str, str], list[Neighbour]] = OrderedDict()
_RESULTS_MAX = 4096
_MEANING: OrderedDict[tuple[UUID, str, str, str], tuple[float, dict[str, float]]] = OrderedDict()
_MEANING_MAX = 4096


def clear_caches() -> None:
    """Forgets every index and result (tests, settings change)."""
    with _LOCK:
        _INDEXES.clear()
        _RESULTS.clear()
        _MEANING.clear()


def _signature(ws: Workspace, space_id: UUID, blob: Blob) -> str:
    """What changes when a file's profile must be redone: its hash and, for a PDF, an Office
    document or an image, the state of its extracted text (which arrives later)."""
    if _kind(blob.path) not in EXTRACTED:
        return blob.sha
    try:
        stamp = ws.extracted_file(space_id, blob.path).stat().st_mtime_ns
    except OSError:
        stamp = 0
    return f"{blob.sha}:{stamp}"


def _text_of(path: str, data: bytes) -> tuple[str, str] | None:
    """(text, title) of a file read from Git; None if it is not text."""
    kind = "markdown" if _kind(path) == "markdown" else kind_of(path, data)
    if kind not in ("markdown", "text"):
        return None
    text = data[: MAX_CHARS * 4].decode("utf-8", "replace")
    relative = path[len(PREFIX) :]
    if kind == "markdown":
        return text, fast_title(text, _stem_name(relative))
    if path.lower().endswith(HTML):
        text = _TAG.sub(" ", text)
    return text, posixpath.basename(relative)


def _read_profiles(
    ws: Workspace, space_id: UUID, repo: Repository, wanted: list[tuple[Blob, str]]
) -> dict[str, tuple[str, _Profile | None]]:
    """Profiles of the files to (re)read, in batches (bounded memory)."""
    found: dict[str, tuple[str, _Profile | None]] = {}
    from_git = []
    for blob, signature in wanted:
        relative = blob.path[len(PREFIX) :]
        kind = _kind(blob.path)
        if kind in EXTRACTED:
            text = ws.extracted_text(space_id, blob.path)
            name = posixpath.basename(relative)
            profile = profile_of(text, name) if text and text.strip() else None
            found[relative] = (signature, profile)
        elif blob.size > (MAX_TEXT if kind == "markdown" else MAX_SNIFF):
            found[relative] = (signature, None)
        else:
            from_git.append((blob, signature))
    for start in range(0, len(from_git), 1000):
        batch = from_git[start : start + 1000]
        try:
            data = repo.read_objects([blob.sha for blob, _ in batch])
        except (GitError, OSError, ValueError):
            data = {}
        for blob, signature in batch:
            relative = blob.path[len(PREFIX) :]
            content = data.get(blob.sha)
            parsed = _text_of(blob.path, content) if content is not None else None
            if parsed is None or not parsed[0].strip():
                found[relative] = (signature, None)
            else:
                found[relative] = (signature, profile_of(*parsed))
    return found


def _saved(ws: Workspace, space_id: UUID) -> dict[str, tuple[str, _Profile | None]]:
    """Profiles saved next to the extracted text (a restart does not read the whole wiki
    again); each keeps its signature, checked before use. Damaged or old: ignored."""
    raw = read_json(ws.extract_path(space_id) / SAVED)
    if not isinstance(raw, dict) or raw.get("version") != [SAVED_VERSION, TERMS]:
        return {}
    found: dict[str, tuple[str, _Profile | None]] = {}
    entries = raw.get("profiles")
    for path, entry in (entries if isinstance(entries, dict) else {}).items():
        try:
            if len(entry) == 1:
                found[path] = (str(entry[0]), None)
            else:
                signature, title, terms, counts, length = entry
                found[path] = (
                    str(signature),
                    _Profile(
                        str(title),
                        tuple(sys.intern(str(t)) for t in terms),
                        tuple(int(n) for n in counts),
                        int(length),
                    ),
                )
        except (TypeError, ValueError):
            continue
    return found


def _save(ws: Workspace, space_id: UUID, profiles: dict[str, tuple[str, _Profile | None]]) -> None:
    entries: dict[str, list[object]] = {}
    for path, (signature, found) in profiles.items():
        entries[path] = (
            [signature]
            if found is None
            else [signature, found.title, list(found.terms), list(found.counts), found.length]
        )
    target = ws.extract_path(space_id) / SAVED
    # json.dumps (C encoder) then an atomic write: json.dump, which writes as it encodes in
    # Python, is ten times slower on 20,000 profiles.
    data = json.dumps({"version": [SAVED_VERSION, TERMS], "profiles": entries}).encode()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as error:  # a cache: never a reason to fail
        log.warning("Related pages: profiles not saved (%s)", error)


def _space_index(ws: Workspace, space_id: UUID, repo: Repository, head: str) -> _Index:
    key = str(repo.path)
    with _LOCK:
        known = _INDEXES.get(key)
        if known is not None and known.head == head:
            _INDEXES.move_to_end(key)
            return known
        building = _BUILDING.setdefault(key, threading.Lock())
    with building:  # two simultaneous requests: a single computation, the second one waits
        with _LOCK:
            known = _INDEXES.get(key)
            if known is not None and known.head == head:
                return known
        index = _build(ws, space_id, repo, head, known)
        with _LOCK:
            _INDEXES[key] = index
            _INDEXES.move_to_end(key)
            while len(_INDEXES) > _INDEXES_MAX:
                _INDEXES.popitem(last=False)
        return index


def _build(
    ws: Workspace, space_id: UUID, repo: Repository, head: str, previous: _Index | None
) -> _Index:
    started = time.perf_counter()
    blobs = [b for b in repo.files(CONTENT_DIR, head) if b.path.startswith(PREFIX)]
    known = previous.profiles if previous is not None else _saved(ws, space_id)
    profiles: dict[str, tuple[str, _Profile | None]] = {}
    wanted = []
    for blob in blobs:
        signature = _signature(ws, space_id, blob)
        entry = known.get(blob.path[len(PREFIX) :])
        if entry is not None and entry[0] == signature:
            profiles[blob.path[len(PREFIX) :]] = entry
        else:
            wanted.append((blob, signature))
    profiles |= _read_profiles(ws, space_id, repo, wanted)
    if len(wanted) >= SAVE_AFTER or (previous is None and (wanted or len(known) != len(blobs))):
        _save(ws, space_id, profiles)
    order = sorted(p for p, (_, found) in profiles.items() if found is not None)
    postings: dict[str, array[int]] = {}
    lengths = []
    for position, path in enumerate(order):
        found = profiles[path][1]
        assert found is not None
        lengths.append(found.length)
        for term, count in zip(found.terms, found.counts, strict=True):
            posting = postings.get(term)
            if posting is None:
                posting = postings[term] = array("I")
            posting.append(position << 8 | count)
    raw = links.base_links(repo, head, {b.path for b in blobs})
    out = {
        p[len(PREFIX) :]: {t for t in targets if t != p[len(PREFIX) :]}
        for p, targets in raw.items()
    }
    into: dict[str, set[str]] = {}
    for source, targets in out.items():
        for target in targets:
            into.setdefault(target, set()).add(source)
    log.debug(
        "Related pages: index of %s (%d files, %d read again) in %.2f s",
        repo.path.name,
        len(blobs),
        len(wanted),
        time.perf_counter() - started,
    )
    return _Index(
        head=head,
        files=frozenset(b.path[len(PREFIX) :] for b in blobs),
        profiles=profiles,
        order=order,
        position={p: i for i, p in enumerate(order)},
        postings=postings,
        lengths=lengths,
        average=sum(lengths) / len(lengths) if lengths else 1.0,
        out=out,
        into=into,
    )


# --- Signals ----------------------------------------------------------------------------------


def _words(index: _Index, profile: _Profile) -> dict[str, float]:
    """Closeness by words: BM25 of the profile's terms in each file, relative to what the
    profile itself would get (1: all its terms, in the same proportions).

    Terms only the page carries count in that total without bringing it closer to anything:
    otherwise a page with a single common word found elsewhere would look like a twin of
    every page using it. Very common terms count too, without being followed (their lists
    are the longest, for an almost zero weight)."""
    count = len(index.order)
    if not count:
        return {}
    common = max(30, int(COMMON * count))

    def saturation(occurrences: int, length: int) -> float:
        norm = K1 * (1 - B + B * length / index.average)
        return occurrences * (K1 + 1) / (occurrences + norm)

    total = 0.0
    scores: dict[int, float] = {}
    lengths = index.lengths
    for term, own in zip(profile.terms, profile.counts, strict=True):
        posting = index.postings.get(term)
        frequency = max(len(posting) if posting is not None else 0, 1)
        weight = math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
        total += weight * saturation(own, profile.length)
        if posting is None or frequency > common:
            continue  # term too common: it counts in the total, without bringing closer
        for packed in posting:
            position = packed >> 8
            scores[position] = scores.get(position, 0.0) + weight * saturation(
                packed & 255, lengths[position]
            )
    if total <= 0:
        return {}
    return {index.order[p]: min(1.0, s / total) for p, s in scores.items()}


def _is_hub(index: _Index, page: str) -> bool:
    return len(index.out.get(page, ())) > HUB


def _graph(index: _Index, path: str) -> dict[str, dict[str, float]]:
    """Outgoing ("linked from this page"), incoming ("cites this page") and shared links."""
    found: dict[str, dict[str, float]] = {}
    outgoing = index.out.get(path, set())
    incoming = index.into.get(path, set())
    folder = posixpath.dirname(path)
    for target in outgoing:
        found.setdefault(target, {})["link"] = 1.0
    for source in incoming:
        # The index of a parent folder, or a page that points to everything: already reached.
        name = posixpath.basename(source).lower()
        parent = posixpath.dirname(source)
        index_of_parent = name in FOLDER_INDEXES and (
            not parent or folder == parent or folder.startswith(parent + "/")
        )
        weak = index_of_parent or _is_hub(index, source)
        found.setdefault(source, {})["backlink"] = 0.3 if weak else 1.0
    shared: Counter[str] = Counter()
    for target in outgoing:  # cite the same pages
        sources = index.into.get(target, set())
        if len(sources) <= HUB:
            shared.update(s for s in sources if s != path)
    for source in incoming:  # cited by the same pages
        if not _is_hub(index, source):
            shared.update(t for t in index.out.get(source, ()) if t != path)
    for page, number in shared.items():
        found.setdefault(page, {})["shared"] = min(1.0, number / 3)
    return found


def _folder_bonus(path: str, other: str) -> float:
    here, there = posixpath.dirname(path), posixpath.dirname(other)
    if here == there:
        return 1.0
    if here.startswith(there + "/") or there.startswith(here + "/") or not (here and there):
        return 0.5
    return 0.0


_NEAR_PAGE = """
WITH me AS (
  SELECT avg(embedding) AS v FROM embedding_chunks
  WHERE space_id = :space AND model = :model AND path = :path
), near AS (
  SELECT c.path, max(1 - (c.embedding <=> me.v)) AS sim
  FROM embedding_chunks c, me
  WHERE c.space_id = :space AND c.model = :model AND c.path <> :path AND me.v IS NOT NULL
  GROUP BY c.path
)
SELECT path, sim, (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY sim) FROM near)
FROM near ORDER BY sim DESC LIMIT :limit
"""

_NEAR_VECTOR = """
WITH near AS (
  SELECT path, max(1 - (embedding <=> CAST(:vector AS vector))) AS sim
  FROM embedding_chunks WHERE space_id = :space AND model = :model
  GROUP BY path
)
SELECT path, sim, (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY sim) FROM near)
FROM near ORDER BY sim DESC LIMIT :limit
"""


def _near(session: Session, query: str, params: dict[str, object]) -> dict[str, float] | None:
    """Neighbours by meaning, scaled to the space: 0 for the median page (or below), 1 for an
    identical vector. Empty if the page has no vector (yet); None if the scan fails or
    exceeds its timeout."""
    try:
        with session.begin_nested():
            # Timeout specific to this query: restored afterwards, or rolled back with the
            # savepoint if it fails.
            before = session.execute(sql("SELECT current_setting('statement_timeout')")).scalar()
            session.execute(
                sql("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(MEANING_TIMEOUT_MS)},
            )
            rows = session.execute(sql(query), params).all()
            session.execute(
                sql("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(before)}
            )
    except SQLAlchemyError as error:
        log.warning("Related pages: meaning unavailable (%s)", type(error).__name__)
        return None
    found: dict[str, float] = {}
    for path, similarity, median in rows:
        base = float(median) if median is not None else 0.0
        if base >= 0.999:
            continue
        value = (float(similarity) - base) / (1 - base)
        if value > 0.05:  # barely above average: not "the same topic"
            found[str(path)] = min(1.0, value)
    return found


def _meaning_of_page(
    session: Session, space: Space, config: EmbeddingConfig, path: str, sha: str
) -> dict[str, float] | None:
    """Neighbours by meaning of a page, kept for an hour per hash (two minutes if it has no
    vector yet: indexing follows the commit). None: failure, nothing kept."""
    key = (space.id, path, sha, config.model)
    now = time.monotonic()
    with _LOCK:
        cached = _MEANING.get(key)
        if cached is not None and now < cached[0]:
            _MEANING.move_to_end(key)
            return cached[1]
    found = _near(
        session,
        _NEAR_PAGE,
        {"space": space.id, "model": config.model, "path": path, "limit": MEANING_CANDIDATES},
    )
    if found is not None:
        with _LOCK:
            _MEANING[key] = (now + (MEANING_TTL if found else MEANING_RETRY), found)
            while len(_MEANING) > _MEANING_MAX:
                _MEANING.popitem(last=False)
    return found


def _combine(
    index: _Index,
    path: str | None,
    words: dict[str, float],
    graph: dict[str, dict[str, float]],
    meaning: dict[str, float] | None,
    exclude: Iterable[str] = (),
) -> list[Neighbour]:
    """Score of each page (weighted signals, added up), from closest to least close."""
    skipped = set(exclude) | ({path} if path else set())
    weights = dict(WEIGHTS)
    if meaning is not None:
        weights["words"] = WORDS_WITH_MEANING
    candidates = set(words) | set(graph) | set(meaning or {})
    outgoing = index.out.get(path, set()) if path else set()
    found = []
    for other in candidates:
        if other in skipped or other not in index.files:
            continue
        profile = index.profile(other)
        if profile is None:
            continue  # file without text (image without extracted text, binary)
        signals = dict(graph.get(other, {}))
        if other in words:
            signals["words"] = words[other]
        if meaning and other in meaning:
            signals["meaning"] = meaning[other]
        if path is not None and (bonus := _folder_bonus(path, other)):
            signals["folder"] = bonus
        parts = {name: weights[name] * value for name, value in signals.items() if value > 0}
        score = min(1.0, sum(parts.values()))
        if score < THRESHOLD:
            continue
        reasons = tuple(sorted(parts, key=lambda name: (-parts[name], name)))
        by_words, by_meaning = signals.get("words", 0.0), signals.get("meaning", 0.0)
        found.append(
            Neighbour(
                other,
                profile.title,
                round(score, 3),
                reasons,
                other in outgoing,
                round(max(by_words, by_meaning), 3),
                "meaning" if by_meaning > by_words else "words",
            )
        )
    found.sort(key=lambda n: (-n.score, n.path))
    return found


# --- Service interface ------------------------------------------------------------------------


def neighbours(
    session: Session | None,
    ws: Workspace,
    space: Space,
    repo: Repository,
    head: str,
    path: str,
    *,
    limit: int = LIMIT,
    config: EmbeddingConfig | None = None,
) -> list[Neighbour]:
    """Pages related to `path` (relative to content/) at commit `head`, without permission
    checks (the caller did them). `config`: the space's embeddings, None for words and links
    only. Raises KeyError if the file does not exist at that commit."""
    model = config.model if config is not None and session is not None else ""
    key = (str(repo.path), head, path, model)
    with _LOCK:
        cached = _RESULTS.get(key)
        if cached is not None:
            _RESULTS.move_to_end(key)
            return cached[:limit]
    index = _space_index(ws, space.id, repo, head)
    if path not in index.files:
        raise KeyError(path)
    profile = index.profile(path)
    words = _words(index, profile) if profile is not None else {}
    meaning = None
    failed = False
    if model and session is not None and config is not None:
        signature = index.profiles.get(path, ("", None))[0]
        meaning = _meaning_of_page(session, space, config, path, signature)
        failed = meaning is None
    # Without a vector for the page (not indexed yet), meaning says nothing: words and links
    # then keep their full weight.
    found = _combine(index, path, words, _graph(index, path), meaning or None)[:MAX_LIMIT]
    if not failed:  # a failed scan will be retried on the next call
        with _LOCK:
            _RESULTS[key] = found
            while len(_RESULTS) > _RESULTS_MAX:
                _RESULTS.popitem(last=False)
    return found[:limit]


def text_neighbours(
    session: Session | None,
    ws: Workspace,
    space: Space,
    repo: Repository,
    head: str,
    title: str,
    text: str,
    *,
    limit: int = LIMIT,
    config: EmbeddingConfig | None = None,
    embedder: Embedder | None = None,
    exclude: Iterable[str] = (),
) -> list[Neighbour]:
    """Pages related to a text that is not (yet) in the wiki: an inbox note. Words, and
    meaning if `config` and `embedder` are given (one call to the provider); neither links
    nor folder. No cache: each note is read only once per run."""
    index = _space_index(ws, space.id, repo, head)
    profile = profile_of(text, title)
    words = _words(index, profile) if profile.terms else {}
    meaning = None
    if config is not None and embedder is not None and session is not None:
        try:
            [vector] = embedder(config, [prefixes(config.model)[0] + f"{title}\n\n{text}"[:6000]])
        except (AIError, ValueError):
            vector = None
        if vector:
            meaning = _near(
                session,
                _NEAR_VECTOR,
                {
                    "space": space.id,
                    "model": config.model,
                    "vector": "[" + ",".join(f"{v:.7g}" for v in vector) + "]",
                    "limit": MEANING_CANDIDATES,
                },
            )
    return _combine(index, None, words, {}, meaning or None, exclude)[:limit]


def close_pairs(
    session: Session | None,
    ws: Workspace,
    space: Space,
    repo: Repository,
    head: str,
    pages: Iterable[str],
    *,
    config: EmbeddingConfig | None = None,
    within: Callable[[str], bool] = lambda _: True,
    limit: int = 10,
) -> list[Pair]:
    """Pairs of pages with very close content (likely duplicates), among the neighbours of
    `pages`: each pair once, from closest to least close."""
    best: dict[tuple[str, str], Pair] = {}
    for page in pages:
        try:
            found = neighbours(session, ws, space, repo, head, page, limit=MAX_LIMIT, config=config)
        except KeyError:
            continue
        for neighbour in found:
            if not neighbour.duplicate or not within(neighbour.path):
                continue
            first, second = sorted((page, neighbour.path))
            pair = Pair(first, second, neighbour.closeness, neighbour.close_by)
            if (first, second) not in best or best[first, second].closeness < pair.closeness:
                best[first, second] = pair
    return sorted(best.values(), key=lambda p: (-p.closeness, p.first, p.second))[:limit]


def for_page(
    session: Session,
    actor: Actor,
    ws: Workspace,
    space: Space,
    path: str,
    *,
    limit: int = LIMIT,
) -> list[Neighbour]:
    """Pages related to a page, for whoever can read the space (web, REST, MCP)."""
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    repo = ws.local_repository(space)
    head = repo.head()
    path = path.strip().strip("/").removeprefix(PREFIX)
    if head is None or not path:
        raise not_found("Page not found.")
    config = semantic.config_for(session, ws.box, space)
    try:
        return neighbours(
            session,
            ws,
            space,
            repo,
            head,
            path,
            limit=max(1, min(limit, MAX_LIMIT)),
            config=config,
        )
    except KeyError:
        raise not_found("Page not found.") from None


# --- "Suggest a link" -------------------------------------------------------------------------


def proposal_origin(path: str, target: str) -> str:
    """Origin of a "Link" note: it identifies the proposal (no duplicates)."""
    return f"“See also” panel: {path} → {target}"


def _short(title: str, size: int = 48) -> str:
    title = " ".join(title.split())
    return title if len(title) <= size else title[: size - 1].rstrip() + "…"


def proposed(actor: Actor, ws: Workspace, space: Space, path: str) -> set[str]:
    """Pages already proposed for a link with `path` (notes not filed yet)."""
    prefix = proposal_origin(path, "")
    return {
        item.origin[len(prefix) :]
        for item in deposits.list_items(actor, ws, space)
        if item.origin.startswith(prefix)
    }


def propose_link(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    path: str,
    target: str,
) -> tuple[str, bool]:
    """Deposits a "Link X and Y" note in the inbox (contributor): the nightly tidy-up will add
    the link, with its guardrails. Nothing is written to the wiki. Returns the path of the
    note, and true if the same proposal was already waiting."""
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    path = path.strip().strip("/").removeprefix(PREFIX)
    target = target.strip().strip("/").removeprefix(PREFIX)
    if not path or not target or path == target:
        raise invalid("Choose two different pages to link.")
    repo = ws.local_repository(space)
    head = repo.head()
    assert head is not None  # local_repository refuses a repository without a commit
    index = _space_index(ws, space.id, repo, head)
    if path not in index.files or target not in index.files:
        raise not_found("Page not found.")
    origin = proposal_origin(path, target)
    for item in deposits.list_items(actor, ws, space):
        if item.origin == origin:
            return item.path, True
    here, there = index.profile(path), index.profile(target)
    title_here = here.title if here else posixpath.basename(path)
    title_there = there.title if there else posixpath.basename(target)
    label = next(
        (
            n.label
            for n in neighbours(session, ws, space, repo, head, path, limit=MAX_LIMIT)
            if n.path == target
        ),
        "",
    )
    link = posixpath.relpath(target, posixpath.dirname(path) or ".")
    why = f" ({label})" if label else ""
    back = (
        "" if path in index.out.get(target, set()) else f", and a link back from {target} if useful"
    )
    body = (
        f"“{title_here}” ({path}) and “{title_there}” ({target}) are related{why}, but the "
        f"first does not point to the second.\n\n"
        f"To do: in {path}, add a link to [{title_there}]({link}) where the topic comes up "
        f"(or in a “See also” section){back}. Only link: do not merge or rewrite the "
        "pages.\n\n"
        "Suggested from the wiki's “See also” panel."
    )
    note = deposits.deposit(
        session,
        database,
        actor,
        ws,
        space,
        title=f"Link “{_short(title_here)}” and “{_short(title_there)}”",
        body=body,
        source="web",
        origin=origin,
        hint=path,
    )
    return note, False
