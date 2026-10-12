"""Text of a PowerPoint 97-2003 file (.ppt), read in pure Python.

A .ppt is an OLE2 compound file (opened with olefile) whose `PowerPoint Document` stream is a
tree of records, each with an 8-byte header: version and instance (2 bytes; version 0xF marks
a container, whose body is more records), type (2 bytes) and length (4 bytes). The text sits
in `TextCharsAtom` (UTF-16LE) and `TextBytesAtom` (one byte per character) records:

- in the outline of the document (`SlideListWithText`), where PowerPoint keeps the text of the
  title and body placeholders, the drawing of the slide only pointing to it
  (`OutlineTextRefAtom`);
- in the drawing of each slide (OfficeArt shapes, `ClientTextbox`): free text boxes, table
  cells, and all the text of a file saved by LibreOffice (which writes no outline);
- in the drawing of the notes page of each slide: the speaker notes.

Slides are found and ordered as PowerPoint does: `Current User` points to the last edit
(`UserEditAtom`), whose chain of persist directories says where each object (document, slide,
notes page) starts in the stream; the outline of the document lists the slides in order.
Masters and layouts are never read, so their prompts ("Click to edit Master title style") stay
out. If that structure is damaged, the stream is read from start to end instead.

Everything is bounded: each length is checked against its container and the stream, and the
nesting depth, the number of records, the length of the text and the time are capped. A
damaged file, or one that is not OLE2, gives no text (and a note), never an exception; an
encrypted one raises `Encrypted`.

Only the standard library is imported at load time (olefile when reading), as in `extract`.
"""

import io
import re
import struct
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

# Record types ([MS-PPT] 2.13.24, and [MS-ODRAW] for the OfficeArt ones, 0xF000 and above).
DOCUMENT = 0x03E8
SLIDE = 0x03EE
NOTES = 0x03F0
NOTES_ATOM = 0x03F1
SLIDE_PERSIST = 0x03F3  # SlidePersistAtom, NotesPersistAtom, MasterPersistAtom
MAIN_MASTER = 0x03F8
OUTLINE_TEXT_REF = 0x0F9E
TEXT_HEADER = 0x0F9F
TEXT_CHARS = 0x0FA0
TEXT_BYTES = 0x0FA8
SLIDE_LIST = 0x0FF0  # SlideListWithText: instance 0 slides, 1 masters, 2 notes pages
USER_EDIT = 0x0FF5
PERSIST_DIRECTORY = 0x1772
CRYPT_SESSION = 0x2F14  # CryptSession10Container: the document is encrypted
GROUP = 0xF003  # OfficeArtSpgrContainer: a group of shapes (a table is one)
SHAPE = 0xF004  # OfficeArtSpContainer
PROPERTIES, MORE_PROPERTIES = 0xF00B, 0xF122  # OfficeArtFOPT, OfficeArtTertiaryFOPT
CHILD_ANCHOR = 0xF00F  # place of a shape in its group: left, top, right, bottom
TABLE_PROPERTIES = 0x039F  # property of a group: bit 0, "is a table"
CONTAINER = 0xF
SLIDES, NOTES_PAGES = 0, 2  # instances of SlideListWithText

ENCRYPTED_TOKEN = 0xF3D1C4DF  # headerToken of Current User when the document is encrypted
MAX_STREAM = 128 * 1024 * 1024  # bytes of the `PowerPoint Document` stream read
MAX_DEPTH = 32  # nesting of containers (a real file goes about 10 deep, groups add a few)
MAX_RECORDS = 2_000_000  # records visited, all walks together
MAX_EDITS = 4096  # edits followed back in the chain of a "fast saved" file
MAX_TEXT = 20_000_000  # characters of text kept
CHECK_EVERY = 4096  # records between two looks at the deadline

# Prompts of masters and layouts (PowerPoint's and LibreOffice's), only met when the structure
# is damaged and the whole stream is read; "*" is the field of a slide number or a date.
PROMPTS = frozenset(
    {
        "Click to edit Master title style",
        "Click to edit Master subtitle style",
        "Click to edit Master text styles",
        "Second level",
        "Third level",
        "Fourth level",
        "Fifth level",
        "Click to edit the title text format",
        "Click to edit the outline text format",
        "Click to edit the notes format",
        *(
            f"{level} Outline Level"
            for level in ("Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth")
        ),
        "Ninth Outline Level",
    }
)
FIELD = "*"


class Budget(Protocol):
    """What `extract.Budget` offers: the time left, and notes for the log."""

    def remaining(self) -> float: ...

    def note(self, message: str) -> None: ...


class Encrypted(Exception):
    """The presentation is protected by a password: its text cannot be read."""


class _Stop(Exception):
    """A bound was reached: the text read so far is kept."""


# Bytes 0x80-0x9F of a TextBytesAtom: the format says Latin-1 (the low byte of a UTF-16
# character), where they are control characters; writers that put Windows-1252 there (€,
# curly quotes, dashes) are read as they meant it.
_CP1252 = {
    byte: char for byte in range(0x80, 0xA0) if (char := bytes([byte]).decode("cp1252", "ignore"))
}
_CONTROLS = re.compile(r"[\x00-\x08\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]")


def clean(text: str) -> list[str]:
    """Paragraphs of a text record: `\\r` ends a paragraph, 0x0B is a line break inside one;
    control characters dropped, spaces trimmed, empty lines and lone fields left out."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    lines = (_CONTROLS.sub("", line).strip() for line in text.split("\n"))
    return [line for line in lines if line and line != FIELD]


def decode(kind: int, body: bytes) -> str:
    if kind == TEXT_CHARS:
        return body[: len(body) & ~1].decode("utf-16-le", "replace")
    return body.decode("latin-1").translate(_CP1252)


Record = tuple[int, int, int, int, int]  # offset, version, instance, type, length


@dataclass
class _Reader:
    data: bytes
    budget: Budget | None
    slides: list[list[str]] = field(default_factory=list)  # paragraphs of each slide
    records: int = 0
    chars: int = 0

    def note(self, message: str) -> None:
        if self.budget is not None:
            self.budget.note(message)

    def record(self, offset: int, end: int | None = None) -> Record | None:
        """The record at `offset` if it fits, whole, before `end` (the end of the stream by
        default), else None. Counts it against the bounds."""
        end = len(self.data) if end is None else min(end, len(self.data))
        if offset < 0 or offset + 8 > end:
            return None
        self.records += 1
        if self.records > MAX_RECORDS:
            self.note(f"more than {MAX_RECORDS} records: the rest of the file is skipped")
            raise _Stop
        if self.records % CHECK_EVERY == 0 and self.budget and self.budget.remaining() <= 0:
            self.note("timeout reached: the rest of the file is skipped")
            raise _Stop
        head, kind, length = struct.unpack_from("<HHI", self.data, offset)
        if offset + 8 + length > end:
            return None  # a length that lies: neither this record nor those after it are read
        return offset, head & 0xF, head >> 4, kind, length

    def children(self, start: int, end: int) -> Iterator[Record]:
        """The records that follow one another from `start` to `end`."""
        while (found := self.record(start, end)) is not None:
            yield found
            start += 8 + found[4]

    def inside(self, container: Record) -> Iterator[Record]:
        return self.children(container[0] + 8, container[0] + 8 + container[4])

    def paragraphs(self, kind: int, offset: int, length: int) -> list[str]:
        found = clean(decode(kind, self.data[offset + 8 : offset + 8 + length]))
        self.chars += sum(len(paragraph) + 1 for paragraph in found)
        if self.chars > MAX_TEXT:
            self.note(f"text longer than {MAX_TEXT // 1_000_000} MB: truncated")
            raise _Stop
        return found

    def texts(
        self, container: Record, outline: list[list[str]] | None = None, depth: int = 0
    ) -> list[str]:
        """Paragraphs of a container, in the order of its records, nested ones included. An
        `OutlineTextRefAtom` stands for the outline text of that index, taken from `outline`
        (emptied there, so that it is given once). A table gives one line per row."""
        found: list[str] = []
        for record in self.inside(container):
            offset, version, _, kind, length = record
            if version == CONTAINER:
                if depth + 1 >= MAX_DEPTH:
                    self.note(f"records nested deeper than {MAX_DEPTH}: skipped")
                    continue
                rows = self.table(record, outline, depth + 1) if kind == GROUP else None
                found += self.texts(record, outline, depth + 1) if rows is None else rows
            elif kind in (TEXT_CHARS, TEXT_BYTES):
                found += self.paragraphs(kind, offset, length)
            elif kind == OUTLINE_TEXT_REF and outline is not None and length >= 4:
                index = struct.unpack_from("<i", self.data, offset + 8)[0]
                if 0 <= index < len(outline):
                    found += outline[index]
                    outline[index] = []
        return found

    def table(self, group: Record, outline: list[list[str]] | None, depth: int) -> list[str] | None:
        """A table is a group of shapes, one per cell, the first one (the group itself) saying
        it is a table: its rows, cells separated by tabs. None for any other group."""
        shapes = [r for r in self.inside(group) if r[3] == SHAPE and r[1] == CONTAINER]
        if not shapes or not self.is_table(shapes[0]):
            return None
        rows: dict[int, list[tuple[int, str]]] = {}  # top → (left, text) of its cells
        loose = []  # cells without a place (a damaged table): one line each, at the end
        for shape in shapes[1:]:
            text = " ".join(self.texts(shape, outline, depth + 1))
            anchor = next((r for r in self.inside(shape) if r[3] == CHILD_ANCHOR), None)
            if not text:
                continue
            if anchor is None or anchor[4] < 16:
                loose.append(text)
                continue
            left, top = struct.unpack_from("<ii", self.data, anchor[0] + 8)
            rows.setdefault(top, []).append((left, text))
        lines = ["\t".join(text for _, text in sorted(rows[top])) for top in sorted(rows)]
        return lines + loose

    def is_table(self, shape: Record) -> bool:
        """The shape has the `tableProperties` property, its "is a table" bit set."""
        for offset, _, count, kind, length in self.inside(shape):
            if kind not in (PROPERTIES, MORE_PROPERTIES):
                continue
            for index in range(min(count, length // 6)):
                name, value = struct.unpack_from("<HI", self.data, offset + 8 + 6 * index)
                if name & 0x3FFF == TABLE_PROPERTIES and value & 1:
                    return True
        return False


@dataclass
class _Slide:
    persist: int
    slide_id: int
    outline: list[list[str]] = field(default_factory=list)  # one entry per TextHeaderAtom


def ppt_text(data: bytes, budget: Budget | None = None) -> str:
    """Text of a .ppt: the slides in order, each followed by its speaker notes, a blank line
    between slides. "" for a damaged file or one that is not OLE2; `Encrypted` if the
    presentation is protected by a password."""
    import olefile

    if not olefile.isOleFile(data=data):
        _note(budget, "not an OLE2 file: no text")
        return ""
    try:
        # A file object, always: olefile takes short bytes for the name of a file to open.
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            if ole.exists("EncryptedSummary"):
                raise Encrypted
            user = _stream(ole, "Current User", 4096)
            stream = _stream(ole, "PowerPoint Document", MAX_STREAM)
            if stream is not None and ole.get_size("PowerPoint Document") > MAX_STREAM:
                _note(budget, f"PowerPoint stream over {MAX_STREAM >> 20} MB: only its start read")
    except Encrypted:
        raise
    except Exception as error:  # noqa: BLE001 — olefile has no single error for a damaged file
        _note(budget, f"damaged OLE2 file, no text ({type(error).__name__})")
        return ""
    if stream is None:
        _note(budget, "no PowerPoint Document stream: no text")
        return ""
    return presentation_text(stream, user, budget)


def presentation_text(document: bytes, user: bytes | None, budget: Budget | None = None) -> str:
    """Text of the `PowerPoint Document` stream, `Current User` saying where its last edit is
    (None if the file has no such stream)."""
    token = struct.unpack_from("<I", user, 12)[0] if user is not None and len(user) >= 16 else 0
    if token == ENCRYPTED_TOKEN:
        raise Encrypted
    reader = _Reader(document, budget)
    try:
        if not _structured(reader, user):
            _sequential(reader)
    except _Stop:
        pass
    return "\n\n".join("\n".join(slide) for slide in reader.slides if slide)


def _stream(ole: Any, name: str, limit: int) -> bytes | None:
    if not ole.exists(name):
        return None
    with ole.openstream(name) as stream:
        return bytes(stream.read(limit))


def _note(budget: Budget | None, message: str) -> None:
    if budget is not None:
        budget.note(message)


def _last_edit(reader: _Reader, user: bytes | None) -> Record | None:
    """The `UserEditAtom` Current User points to; if it points elsewhere (or is missing), the
    last one written in the stream."""
    if user is not None and len(user) >= 20:
        found = reader.record(struct.unpack_from("<I", user, 16)[0])
        if found is not None and found[3] == USER_EDIT:
            return found
    last = None
    for found in reader.children(0, len(reader.data)):
        if found[3] == USER_EDIT:
            last = found
    return last


def _persist(reader: _Reader, user: bytes | None) -> tuple[dict[int, int], int] | None:
    """Where each persist object starts (id → offset) and the persist id of the document, the
    edits read from the last back to the first (a newer edit wins). None if there is no edit.
    Raises `Encrypted` if the last edit points to an encryption session."""
    edit = _last_edit(reader, user)
    if edit is None or edit[4] < 20:
        return None
    _, _, document = struct.unpack_from("<III", reader.data, edit[0] + 16)
    crypt = struct.unpack_from("<I", reader.data, edit[0] + 36)[0] if edit[4] >= 32 else 0
    persist: dict[int, int] = {}
    seen: set[int] = set()
    while edit is not None and edit[3] == USER_EDIT and edit[4] >= 20:
        if edit[0] in seen or len(seen) >= MAX_EDITS:
            break
        seen.add(edit[0])
        previous, directory, _ = struct.unpack_from("<III", reader.data, edit[0] + 16)
        table = reader.record(directory)
        if table is not None and table[3] == PERSIST_DIRECTORY:
            position, end = table[0] + 8, table[0] + 8 + table[4]
            while position + 4 <= end:
                entry = struct.unpack_from("<I", reader.data, position)[0]
                first, count = entry & 0xFFFFF, entry >> 20
                position += 4
                for index in range(min(count, (end - position) // 4)):
                    offset = struct.unpack_from("<I", reader.data, position + 4 * index)[0]
                    persist.setdefault(first + index, offset)
                position += 4 * count
        edit = reader.record(previous) if previous else None
    session = reader.record(persist.get(crypt, -1)) if crypt else None
    if session is not None and session[3] == CRYPT_SESSION:
        raise Encrypted
    return persist, document


def _structured(reader: _Reader, user: bytes | None) -> bool:
    """Slides in the order of the outline, read from the objects the last edit points to.
    False if that structure cannot be followed (nothing read then)."""
    found = _persist(reader, user)
    if found is None:
        return False
    persist, document_id = found
    document = reader.record(persist.get(document_id, -1))
    if document is None or document[3] != DOCUMENT or document[1] != CONTAINER:
        return False
    slides: list[_Slide] = []
    notes_pages: list[int] = []
    for listing in reader.inside(document):
        if listing[3] != SLIDE_LIST or listing[1] != CONTAINER:
            continue
        slide: _Slide | None = None
        for offset, _, _, kind, length in reader.inside(listing):
            if kind == SLIDE_PERSIST and length >= 16:
                persist_id, _, _, slide_id = struct.unpack_from("<IIiI", reader.data, offset + 8)
                if listing[2] == SLIDES:
                    slide = _Slide(persist_id, slide_id)
                    slides.append(slide)
                elif listing[2] == NOTES_PAGES:
                    notes_pages.append(persist_id)
            elif listing[2] == SLIDES and slide is not None:
                if kind == TEXT_HEADER:
                    slide.outline.append([])
                elif kind in (TEXT_CHARS, TEXT_BYTES):
                    if not slide.outline:
                        slide.outline.append([])
                    slide.outline[-1] += reader.paragraphs(kind, offset, length)
    if not slides:
        return False
    by_id: dict[int, list[str]] = {}
    for slide in slides:
        record = reader.record(persist.get(slide.persist, -1))
        outline = [list(block) for block in slide.outline]
        drawing: list[str] = []
        if record is not None and record[3] == SLIDE and record[1] == CONTAINER:
            drawing = reader.texts(record, outline)
        # Outline text that no shape points to (a file from another tool) comes first.
        reader.slides.append(_once([p for block in outline for p in block], drawing))
        by_id.setdefault(slide.slide_id, reader.slides[-1])
    for persist_id in notes_pages:
        page = reader.record(persist.get(persist_id, -1))
        if page is None or page[3] != NOTES or page[1] != CONTAINER:
            continue
        atom = next((r for r in reader.inside(page) if r[3] == NOTES_ATOM and r[4] >= 4), None)
        if atom is not None:
            slide_id = struct.unpack_from("<I", reader.data, atom[0] + 8)[0]
            if slide_id in by_id:
                by_id.pop(slide_id).extend(reader.texts(page))
    return True


def _once(outline: list[str], drawing: list[str]) -> list[str]:
    """The paragraphs of a slide: the outline text no shape points to, then the drawing, less
    the copies of that outline text (some tools write a placeholder's text in both). Only one
    copy per occurrence in the outline is dropped: a paragraph the drawing repeats on its own
    (the same word in several cells of a table) is kept every time."""
    copies = Counter(outline)
    kept = list(outline)
    for paragraph in drawing:
        if copies[paragraph] > 0:
            copies[paragraph] -= 1
        else:
            kept.append(paragraph)
    return kept


def _sequential(reader: _Reader) -> None:
    """Damaged structure: the text of the containers in the order of the stream (masters left
    out, their prompts too), each paragraph only once in the whole document."""
    reader.note("PowerPoint structure damaged: text read in the order of the file")
    seen: set[str] = set()
    for container in reader.children(0, len(reader.data)):
        if container[3] == CRYPT_SESSION:
            raise Encrypted
        if container[1] != CONTAINER or container[3] == MAIN_MASTER:
            continue
        reader.slides.append([])
        for paragraph in reader.texts(container):
            if paragraph not in seen and paragraph not in PROMPTS:
                seen.add(paragraph)
                reader.slides[-1].append(paragraph)
