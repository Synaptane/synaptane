"""Links between the pages of a space: link index, and moves that keep links right.

A move no longer breaks any link: `move_changes` is the only function that computes it,
shared by the tidy-up agent (on its in-memory working view), the MCP, the REST API and the
web (on the repository). The moved page keeps its outgoing links right from its new
location, and the pages that pointed to it are rewritten in the same commit.

Paths: keys and files are repository paths (`content/…`); link targets are relative to
`content/` (as in the wiki).
"""

import posixpath
from collections.abc import Callable, Iterable, Mapping

from synaptane.core.markdown import WikiIndex, links_fast, moved_path, rewrite_links
from synaptane.git.repo import GitError, Repository
from synaptane.services.inbox import CONTENT_DIR

PREFIX = CONTENT_DIR + "/"
MARKDOWN = (".md", ".markdown")

# Link index per repository clone, kept from one call to the next (chained runs, moves, wiki
# health): only the pages changed since the previous commit are read again.
_LINKS: dict[str, tuple[str, dict[str, set[str]]]] = {}


def names(files: Iterable[str]) -> WikiIndex:
    """Index of file names (wiki links), paths relative to content/."""
    return WikiIndex.of(p[len(PREFIX) :] for p in files if p.startswith(PREFIX))


def base_links(repo: Repository, base: str, files: set[str]) -> dict[str, set[str]]:
    """Outgoing links of each Markdown page at commit `base` (`files`: its files)."""
    pages = {p for p in files if p.endswith(".md")}
    known = _LINKS.get(str(repo.path))
    changed: set[str] = set()
    if known is not None and known[0] != base:
        try:
            changed = repo.changed_paths(known[0], base)
        except GitError:
            known = None
    if known is None:
        to_read, links = pages, {}
    elif known[0] == base:
        return dict(known[1])
    else:
        links = {p: t for p, t in known[1].items() if p in pages and p not in changed}
        to_read = {p for p in pages if p in changed or p not in links}
    index = names(files)
    # Read by hash: on thousands of pages, much faster than by path.
    shas = {b.path: b.sha for b in repo.files(CONTENT_DIR, base)}
    by_sha = repo.read_objects([shas[p] for p in to_read if p in shas])
    contents = {p: by_sha[shas[p]] for p in to_read if shas.get(p) in by_sha}
    contents |= repo.read_many(sorted(set(to_read) - set(contents)), base)
    for path, data in contents.items():
        links[path] = links_fast(data.decode("utf-8", "replace"), path[len(PREFIX) :], index)
    _LINKS.pop(str(repo.path), None)
    _LINKS[str(repo.path)] = (base, links)
    while len(_LINKS) > 8:  # a few recent spaces only
        _LINKS.pop(next(iter(_LINKS)))
    return dict(links)


ReadMany = Callable[[list[str]], Mapping[str, bytes]]


def _stem(path: str) -> str:
    return posixpath.basename(path).lower().removesuffix(".md")


def move_changes(
    read: ReadMany, files: set[str], links: Mapping[str, set[str]], old: str, new: str
) -> dict[str, bytes]:
    """Writes for a move from `old` to `new`: the file at its new path (outgoing links
    recomputed if it is a Markdown page) and each page whose links to it change. Deleting
    `old` is left to the caller, which writes everything at once.

    `files`: files before the move; `links`: outgoing links of each page before the move
    (see `base_links`, `Overlay.links`); `read`: content of several files.

    The pages examined are those pointing to the moved file or to a file of the same name: a
    wiki link `[[name]]` can change target when a file of the same name comes closer, and it
    is then made explicit (`[[folder/name]]`) to keep its own."""
    before, after = old[len(PREFIX) :], new[len(PREFIX) :]
    moves = {before: after}
    old_index = names(files)
    new_index = names((files - {old}) | {new})
    stems = {_stem(before), _stem(after)}
    watched = {p for p in old_index.paths if _stem(p) in stems} | {before}
    pages = sorted(
        page
        for page, targets in links.items()
        if page not in (old, new) and page.endswith(MARKDOWN) and targets & watched
    )
    contents = read([old, *pages])
    data = contents.get(old)
    if data is None:
        raise KeyError(old)
    writes = {new: _rewritten(data, before, after, moves, old_index, new_index)}
    for page in pages:
        if (text := contents.get(page)) is None:
            continue
        here = page[len(PREFIX) :]
        rewritten = _rewritten(text, here, moved_path(here, moves), moves, old_index, new_index)
        if rewritten != text:
            writes[page] = rewritten
    return writes


def _rewritten(
    data: bytes,
    before: str,
    after: str,
    moves: Mapping[str, str],
    old_index: WikiIndex,
    new_index: WikiIndex,
) -> bytes:
    """Content of a page with its links rewritten; unchanged if it is not UTF-8 Markdown
    (never rewrite what cannot be read back identically)."""
    if not before.endswith(MARKDOWN):
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    rewritten = rewrite_links(text, before, after, moves, old_index, new_index)
    return data if rewritten == text else rewritten.encode("utf-8")
