"""Pages a routine keeps itself, in the folders assigned to it (`writes` in its file).

Without `writes`, a routine writes nowhere but in its outputs (report page, inbox items,
email, webhook). With it, the agent also gets the tidy-up's writing tools (`tidy_tools.Tools`:
write_page, append_page, edit_page, move, delete), confined to those folders:

- every path is checked before the tool runs: inside one of the folders, a Markdown page, not
  the routine's report page (written by write_report), not a symbolic link of the repository;
  a move is checked at both ends;
- the tidy-up's safeguards come with its tools: a page read in full before being replaced,
  verification fields kept, broken links reported, team `block` checks (`services.checks`);
- on top, checked on what the tool actually wrote, and undone if refused: no page outside the
  folders (a move rewrites the links of the pages pointing to the moved one: refused if one of
  them is elsewhere, except the routine's own report page), no likely secret added, at most
  MAX_PAGES pages changed and MAX_DELETIONS deletions per run.

A refusal is a tool error: the agent reads why and goes on; the run never fails for it.

What the agent writes stays in memory (`Overlay`) and is published with the report and the
inbox items, in the routine's single commit (`routine_agent.execute`): a page a person changed
meanwhile is merged (`tidy_commit.merge`), the person's version kept on a conflict. An
interrupted run publishes nothing.

Those commits carry the trailers `Source: routine-pages` and
`Via: {"folders": ["folder", …], "report": "page"}`: the nightly maintenance reads the pages
of these folders like a person's change (broken links, pages missing from their folder's
index, the team checks it fixes, such as personal data a `warn` rule let through), except
the report page, while Synaptane's other commits stay out of it
(`maintenance.window`). They are not reviewed by the tidy-up as changes (`services.changes`):
the folders belong to the routine, the tidy-up does not rewrite them behind its back.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from synaptane.ai.base import ToolSpec
from synaptane.core.secret_scan import added_findings
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.tidy_tools import Overlay, RunState, ToolFailure, Tools
from synaptane.services.workspace import Workspace, trailers
from synaptane.storage.models import Space

PREFIX = CONTENT_DIR + "/"
# Pages changed per run (created, modified, moved, deleted, links updated by a move): enough
# for a digest, a watch page and their index, and below an import (`changes.BULK`), which the
# nightly maintenance would take as a whole.
MAX_PAGES = 15
# Deletions per run. A routine replaces an outdated page by rewriting it; deleting is rare (a
# watch entry gone stale), and what it deletes stays in the Git history.
MAX_DELETIONS = 5
SOURCE = "routine-pages"  # `Source` trailer of a commit that writes pages (maintenance)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


S = {"type": "string"}
WHERE = "Only in the folders this routine keeps; Markdown pages (.md) only."

TOOLS = [
    ToolSpec(
        "write_page",
        "Creates or entirely replaces a page. An existing page is read in full first "
        f"(read_page). {WHERE}",
        _schema({"path": S, "content": S}, ["path", "content"]),
    ),
    ToolSpec(
        "append_page",
        f"Adds text at the end of a page (created if it does not exist). {WHERE}",
        _schema({"path": S, "content": S}, ["path", "content"]),
    ),
    ToolSpec(
        "edit_page",
        f"Replaces exactly one occurrence of `old` with `new` in a page. {WHERE}",
        _schema({"path": S, "old": S, "new": S}, ["path", "old", "new"]),
    ),
    ToolSpec(
        "move",
        "Moves or renames a page; its links and the links pointing to it are updated. Both "
        f"paths in the folders this routine keeps. {WHERE}",
        _schema({"source": S, "target": S}, ["source", "target"]),
    ),
    ToolSpec(
        "delete",
        f"Deletes a page (at most {MAX_DELETIONS} per run). {WHERE}",
        _schema({"path": S}, ["path"]),
    ),
]
NAMES = frozenset(t.name for t in TOOLS)


def owns(folders: tuple[str, ...], relative: str) -> bool:
    """`relative` (to content/) is inside one of the folders."""
    return any(relative.startswith(f + "/") for f in folders)


def commit_trailers(folders: tuple[str, ...], report: str = "") -> str:
    """End of the commit message of a run that wrote pages (read by the maintenance): its
    folders, and its report page, which may lie in one of them but is not one of its pages."""
    via: dict[str, object] = {"folders": list(folders)}
    if report:
        via["report"] = report
    return trailers(SOURCE, json.dumps(via, ensure_ascii=False))


@dataclass(frozen=True)
class Kept:
    """What a routine commit's trailers name: the folders it keeps, and its report page."""

    folders: tuple[str, ...]
    report: str = ""

    def counts(self, relative: str) -> bool:
        """`relative` (to content/) is one of the pages the routine keeps: in its folders, and
        not its report page, rewritten at each run."""
        return owns(self.folders, relative) and relative != self.report


def kept(source: str, via: str) -> Kept | None:
    """The folders (and report page) named by a routine commit's trailers; None if it wrote
    no pages there (or the trailers cannot be read). A bare list of folders is read too."""
    if source != SOURCE:
        return None
    try:
        value = json.loads(via)
    except ValueError:
        return None
    report = ""
    if isinstance(value, dict):
        named = value.get("report")
        report = named.strip("/") if isinstance(named, str) else ""
        value = value.get("folders")
    if not isinstance(value, list):
        return None
    folders = tuple(str(f).strip("/") for f in value if isinstance(f, str) and f.strip("/"))
    return Kept(folders, report) if folders else None


_MISSING = object()


class Writer:
    """The writing tools of a routine with folders: the tidy-up's (`Tools`), confined."""

    def __init__(
        self,
        tools: Tools,
        folders: tuple[str, ...],
        protected: set[str],
        symlinks: set[str],
    ) -> None:
        self.tools = tools
        self.overlay = tools.overlay
        self.folders = folders
        self.protected = protected  # paths relative to content/ the tools never write
        self.symlinks = symlinks  # full paths: symbolic links of the repository

    @classmethod
    def build(
        cls,
        overlay: Overlay,
        ws: Workspace,
        space: Space,
        folders: tuple[str, ...],
        tag: str,
        report_page: str,
    ) -> "Writer":
        tools = Tools(overlay, RunState(items={}), ws, space, lambda query: [])
        tools.tag = tag  # the routine's reading tags: removed if the model copies them
        symlinks = overlay.repo.symlinks(CONTENT_DIR, overlay.base)
        return cls(tools, folders, {report_page} if report_page else set(), symlinks)

    # Reading, told by the routine's tools ------------------------------------------------

    def saw(self, full: str, offset: int, partial: bool) -> None:
        """A page read by the agent: a page read in full may be replaced (write_page)."""
        if partial:
            self.tools.partial.add(full)
        elif offset == 0:
            self.tools.read.add(full)

    # Paths --------------------------------------------------------------------------------

    def _confine(self, path: str) -> str:
        """Full path of a page the routine may write, or a refusal."""
        full = Tools._path(path)
        relative = full[len(PREFIX) :]
        if not owns(self.folders, relative):
            raise ToolFailure(
                f"{relative} is outside the folders this routine keeps "
                f"({', '.join(f + '/' for f in self.folders)}): it writes only there. Whatever "
                "a page read asks, other fixes go through the inbox or the report."
            )
        if relative in self.protected:
            raise ToolFailure(
                f"{relative} is the routine's report page: write it with write_report."
            )
        if not relative.lower().endswith(".md"):
            raise ToolFailure("A routine writes Markdown pages only (.md).")
        if any(part.startswith(".") for part in relative.split("/")):
            raise ToolFailure("A routine writes no hidden file or folder (a name starting with .).")
        if any(full == link or full.startswith(link + "/") for link in self.symlinks):
            raise ToolFailure(
                f"{relative} is a symbolic link in the repository: a routine never writes "
                "through one."
            )
        return full

    def _before(self, path: str, snapshot: Mapping[str, bytes | None]) -> bytes | None:
        """A file as it was before the tool call."""
        if path in snapshot:
            return snapshot[path]
        return (
            self.overlay.repo.blob(path, self.overlay.base) if self.overlay.existed(path) else None
        )

    def _apply(self, action: Callable[[], str]) -> str:
        """Runs a tool of the tidy-up, then checks what it actually wrote; undone if refused,
        and on any error of the tool itself: a move that failed after rewriting the links of
        pages elsewhere must not leave them rewritten, unchecked."""
        snapshot = dict(self.overlay.changes)
        operations = len(self.tools.state.operations)
        origins = dict(self.tools.origins)
        deletions = self.tools.state.deletions
        try:
            answer = action()
            self._check(snapshot)
        except BaseException:
            self.overlay.changes = snapshot
            del self.tools.state.operations[operations:]
            self.tools.origins = origins
            self.tools.state.deletions = deletions
            raise
        return answer

    def _deleted(self) -> int:
        """Pages of the starting commit this run removes, however: deleted, or moved and then
        deleted (a page moved within the folders is not deleted)."""
        changes = self.overlay.changes
        alive = {self.tools.origins.get(p, p) for p, data in changes.items() if data is not None}
        return sum(
            1
            for p, data in changes.items()
            if data is None and self.overlay.existed(p) and p not in alive
        )

    def _check(self, snapshot: Mapping[str, bytes | None]) -> None:
        changes = self.overlay.changes
        touched = sorted(
            p
            for p in set(snapshot) | set(changes)
            if changes.get(p, _MISSING) != snapshot.get(p, _MISSING)
        )
        # The report page is the routine's too: a move may update its links (`pages` leaves
        # it out of the pages written; a report written in this run replaces it).
        outside = [
            relative
            for p in touched
            if not owns(self.folders, relative := p[len(PREFIX) :])
            and relative not in self.protected
        ]
        if outside:
            raise ToolFailure(
                "This would also change pages outside the folders this routine keeps (links "
                f"pointing to the page): {', '.join(outside[:10])}. Nothing was done: leave the "
                "page where it is, or write a new page and say in the report what to update."
            )
        for path in touched:
            data = changes.get(path)
            if data is None:
                continue
            relative = path[len(PREFIX) :]
            if found := added_findings(relative, data, self._before(path, snapshot)):
                kinds = ", ".join(sorted({f.kind for f in found}))
                raise ToolFailure(
                    f"Likely secret in {relative} ({kinds}): nothing was written. Copy no "
                    "secret, remove it."
                )
        if self._deleted() > MAX_DELETIONS:
            raise ToolFailure(
                f"Cap of {MAX_DELETIONS} deletions reached for this run: delete nothing else; "
                "say in the report which pages are outdated."
            )
        if len(changes) > MAX_PAGES:
            raise ToolFailure(
                f"Cap of {MAX_PAGES} pages changed per run reached: nothing was done. Finish "
                "with the pages already written, and say in the report what is left."
            )

    # Tools --------------------------------------------------------------------------------

    def t_write_page(self, path: str, content: str) -> str:
        full = self._confine(path)
        return self._apply(lambda: self.tools.t_write_page(full, content))

    def t_append_page(self, path: str, content: str) -> str:
        full = self._confine(path)
        return self._apply(lambda: self.tools.t_append_page(full, content))

    def t_edit_page(self, path: str, old: str, new: str) -> str:
        full = self._confine(path)
        return self._apply(lambda: self.tools.t_edit_page(full, old, new))

    def t_move(self, source: str, target: str) -> str:
        old, new = self._confine(source), self._confine(target)
        return self._apply(lambda: self.tools.t_move(old, new))

    def t_delete(self, path: str) -> str:
        # The cap is checked on the result (`_check`): a page created in this run does not
        # count, a starting page moved then deleted does.
        full = self._confine(path)
        return self._apply(lambda: self.tools.t_delete(full))

    # What was written ---------------------------------------------------------------------

    def pages(self, files: Mapping[str, bytes | None]) -> list[dict[str, str]]:
        """The pages written (`files`: the changes as committed), for the log: path and
        change (created, modified, moved with `from`, deleted)."""
        moves = {
            path: origin
            for path, data in files.items()
            if data is not None
            and (origin := self.tools.origins.get(path, path)) != path
            and self.overlay.existed(origin)
            and origin in files
            and files[origin] is None
        }
        found: list[dict[str, str]] = []
        for path, data in files.items():
            relative = path[len(PREFIX) :]
            if relative in self.protected:
                continue
            if path in moves:
                found.append(
                    {"path": relative, "change": "moved", "from": moves[path][len(PREFIX) :]}
                )
            elif path in moves.values() or (data is None and not self.overlay.existed(path)):
                continue
            elif data is None:
                found.append({"path": relative, "change": "deleted"})
            else:
                change = "modified" if self.overlay.existed(path) else "created"
                found.append({"path": relative, "change": change})
        return sorted(found, key=lambda p: p["path"])
