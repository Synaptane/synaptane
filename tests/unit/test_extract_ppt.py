"""PowerPoint 97-2003 (.ppt), read in Python by `extract_ppt`.

Real files: one written by LibreOffice (scripts/legacy_files.py), two by PowerPoint (from
Apache POI, see tests/fixtures/legacy/README.md). The structure and the edge cases are tested
on `PowerPoint Document` streams built here record by record."""

import io
import random
import struct
from pathlib import Path

import olefile
import pytest

from synaptane import extract_ppt
from synaptane.extract import Budget, ExtractionFailed, extract_direct, run_isolated
from synaptane.extract_ppt import Encrypted, ppt_text, presentation_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def read(name: str) -> str:
    """Text of a fixture, read like the worker reads it: in the bounded child process."""
    result = run_isolated("office", ".ppt", fixture(name), languages="eng", ocr_images=False)
    assert result.text is not None
    return result.text


# --- Real files --------------------------------------------------------------------------------


def test_libreoffice_file_gives_titles_text_boxes_tables_and_notes() -> None:
    """LibreOffice writes everything in the drawing of the slides, nothing in the outline: catppt
    found no text at all there."""
    assert read("kickoff-slides.ppt") == (
        "Project Kilimanjaro kickoff\n"
        "Acme IT team · new meeting rooms\n"
        "\n"
        "Goals\n"
        "Video conferencing in the four meeting rooms\n"
        "One remote for everything\n"
        "Pilot room: Serengeti, third floor\n"  # free text box
        "\n"
        "Budget\n"
        "Screens and cameras: € 12,000\n"
        "Cabling: done by the Acme IT team\n"
        "Ask the Zambezi supplier for a second quote before Friday.\n"  # speaker notes
        "\n"
        "Rooms\n"
        "Room\tSeats\tScreen\n"  # a table: one line per row
        "Kalahari\t12\t75 inch\n"
        "Okapi\t6\t55 inch"
    )


def test_powerpoint_file_gives_its_outline_and_notes_once() -> None:
    """PowerPoint keeps titles and bodies in the outline, the slides pointing to it; the masters
    ("Click to edit Master title style") are left out."""
    assert read("poi-basic-test.ppt") == (
        "This is a test title\n"
        "This is a test subtitle\n"
        "This is on page 1\n"
        "These are the notes for page 1\n"
        "\n"
        "This is the title on page 2\n"
        "This is page two\n"
        "It has several blocks of text\n"
        "None of them have formatting\n"
        "These are the notes on page two, again lacking formatting"
    )


def test_password_protected_file_is_a_failure_saying_so() -> None:
    with pytest.raises(ExtractionFailed, match="^protected by a password, no text$"):
        read("poi-password-protected.ppt")
    with pytest.raises(Encrypted):  # EncryptedSummary stream, and the token of Current User
        ppt_text(fixture("poi-password-protected.ppt"))


def test_damaged_or_foreign_files_give_no_text() -> None:
    budget = Budget()
    for data in (
        fixture("kickoff-slides.ppt")[:3000],
        fixture("kickoff-slides.ppt")[:-1000],
        fixture("spare-parts.xls"),  # OLE2, but no PowerPoint Document stream
        b"PK\x03\x04 a .pptx renamed" * 200,
        b"",
    ):
        assert ppt_text(data, budget) == ""
    assert budget.notes == [
        "damaged OLE2 file, no text (OleFileError)",
        "no PowerPoint Document stream: no text",
        "not an OLE2 file: no text",
    ]
    # Through the child: no failure for a damaged file, the usual refusal for a foreign one.
    result = run_isolated(
        "office", ".ppt", fixture("kickoff-slides.ppt")[:3000], languages="eng", ocr_images=False
    )
    assert result.text == "" and result.notes == ("damaged OLE2 file, no text (OleFileError)",)
    with pytest.raises(ExtractionFailed, match=r"not a \.ppt file from Office 97-2003"):
        extract_direct("office", ".ppt", b"PK\x03\x04 a .pptx renamed", "eng", False, Budget())


def test_random_damage_never_raises() -> None:
    """Bytes changed or cut anywhere in the stream: some text or none, never an exception."""
    budget = Budget()
    with olefile.OleFileIO(io.BytesIO(fixture("poi-basic-test.ppt"))) as ole:
        document = ole.openstream("PowerPoint Document").read()
        user = ole.openstream("Current User").read()
    rng = random.Random(7)
    for _ in range(300):
        damaged = bytearray(document)
        for _ in range(rng.choice((1, 10, 100))):
            damaged[rng.randrange(len(damaged))] = rng.randrange(256)
        cut = rng.randrange(len(damaged)) if rng.random() < 0.3 else len(damaged)
        assert isinstance(presentation_text(bytes(damaged[:cut]), user, budget), str)


# --- Streams built record by record ------------------------------------------------------------

DOCUMENT, SLIDE, NOTES, MAIN_MASTER = 0x03E8, 0x03EE, 0x03F0, 0x03F8
SLIDE_ATOM, NOTES_ATOM, PERSIST_ATOM, DRAWING = 0x03EF, 0x03F1, 0x03F3, 0x040C
LIST, TEXT_HEADER, CHARS, BYTES, OUTLINE_REF = 0x0FF0, 0x0F9F, 0x0FA0, 0x0FA8, 0x0F9E
EDIT, DIRECTORY, CRYPT = 0x0FF5, 0x1772, 0x2F14
DG, GROUP, SHAPE, OPT, ANCHOR, TEXTBOX = 0xF002, 0xF003, 0xF004, 0xF00B, 0xF00F, 0xF00D


def atom(kind: int, body: bytes = b"", instance: int = 0) -> bytes:
    return struct.pack("<HHI", instance << 4, kind, len(body)) + body


def box(kind: int, *children: bytes, instance: int = 0) -> bytes:
    body = b"".join(children)
    return struct.pack("<HHI", (instance << 4) | 0xF, kind, len(body)) + body


def chars(text: str) -> bytes:
    return atom(CHARS, text.encode("utf-16-le"))


def textbox(*content: bytes, anchor: tuple[int, int] | None = None) -> bytes:
    """A shape with text, in a slide drawing (with its place in a group if given)."""
    place = [atom(ANCHOR, struct.pack("<4i", anchor[0], anchor[1], 0, 0))] if anchor else []
    return box(SHAPE, *place, box(TEXTBOX, atom(TEXT_HEADER, bytes(4)), *content))


def drawing(*shapes: bytes) -> bytes:
    return box(DRAWING, box(DG, box(GROUP, box(SHAPE), *shapes)))


def table(*cells: tuple[str, int, int]) -> bytes:
    """A table: a group whose first shape has `tableProperties` (bit 0 set), one shape per
    cell, given as (text, left, top)."""
    properties = atom(OPT, struct.pack("<HI", 0x039F, 1), instance=1)
    shapes = [textbox(chars(text), anchor=(left, top)) for text, left, top in cells]
    return box(GROUP, box(SHAPE, properties), *shapes)


def slide(*shapes: bytes) -> bytes:
    return box(SLIDE, atom(SLIDE_ATOM, bytes(24)), drawing(*shapes))


def notes(slide_id: int, text: str) -> bytes:
    return box(NOTES, atom(NOTES_ATOM, struct.pack("<I", slide_id) + bytes(4)), drawing(
        textbox(chars(text))
    ))  # fmt: skip


def persist_atom(persist_id: int, slide_id: int) -> bytes:
    return atom(PERSIST_ATOM, struct.pack("<IIiII", persist_id, 0, 0, slide_id, 0))


def user_edit(directory: int, *, previous: int = 0, crypt: int = 0) -> bytes:
    """UserEditAtom: previous edit, persist directory, document = persist id 1."""
    body = struct.pack("<IHBBIIIIHH", 0, 0, 0, 3, previous, directory, 1, 10, 1, 0)
    return atom(EDIT, body + (struct.pack("<I", crypt) if crypt else b""))


def current_user(offset: int, token: int = 0xE391C05F) -> bytes:
    return atom(0x0FF6, struct.pack("<IIIHHBBH", 0x14, token, offset, 0, 0x3F4, 3, 0, 0))


def build(
    objects: dict[int, bytes],
    outline: list[tuple[int, int, list[bytes]]],
    notes_pages: list[int] | None = None,
    *,
    crypt: int = 0,
    order: list[int] | None = None,
) -> tuple[bytes, bytes]:
    """(PowerPoint Document, Current User) streams: the document (persist id 1), the objects
    (persist id → record) in `order`, a persist directory and the edit pointing to it.
    `outline`: (persist id, slide id, outline text records) of each slide, in order."""
    slides = [persist_atom(pid, sid) + b"".join(texts) for pid, sid, texts in outline]
    lists = [box(LIST, *slides)]
    if notes_pages:
        lists.append(box(LIST, *(persist_atom(pid, 0) for pid in notes_pages), instance=2))
    stream = box(DOCUMENT, atom(0x03E9, bytes(40)), *lists)
    offsets = {1: 0}
    for persist_id in order or sorted(objects):
        offsets[persist_id] = len(stream)
        stream += objects[persist_id]
    directory = len(stream)
    stream += atom(DIRECTORY, b"".join(
        struct.pack("<II", (1 << 20) | persist_id, offset) for persist_id, offset in offsets.items()
    ))  # fmt: skip
    return stream + user_edit(directory, crypt=crypt), current_user(len(stream))


def outline_text(kind: int, text: str) -> list[bytes]:
    return [atom(TEXT_HEADER, struct.pack("<I", kind)), chars(text)]


def text_of(stream: bytes, user: bytes | None, budget: Budget | None = None) -> str:
    return presentation_text(stream, user, budget or Budget())


def test_slides_follow_the_outline_not_the_stream() -> None:
    """Titles and bodies in the outline, shapes pointing to them; a free text box; slides
    stored in the stream in another order than shown; notes found by the slide they belong
    to; the master left out."""
    master = box(MAIN_MASTER, drawing(textbox(chars("Click to edit Master title style"))))
    stream, user = build(
        {
            2: master,
            3: slide(
                textbox(atom(OUTLINE_REF, struct.pack("<i", 0))),
                textbox(atom(OUTLINE_REF, struct.pack("<i", 1))),
                textbox(chars("Free text box")),
            ),
            4: slide(textbox(atom(OUTLINE_REF, struct.pack("<i", 0)))),
            5: notes(256, "Notes of the first slide"),
        },
        [
            (3, 256, outline_text(0, "First title") + outline_text(1, "Point one\rPoint two")),
            (4, 257, outline_text(0, "Second title")),
        ],
        [5],
        order=[5, 4, 2, 3],
    )
    assert text_of(stream, user) == (
        "First title\nPoint one\nPoint two\nFree text box\nNotes of the first slide\n\nSecond title"
    )


def test_text_in_both_outline_and_drawing_is_given_once() -> None:
    """Some tools write the text of a placeholder in the outline and in the shape too: one copy
    is dropped. What the drawing repeats on its own (table cells, a second "Vote") stays."""
    stream, user = build(
        {
            3: slide(
                textbox(chars("Agenda")),
                textbox(chars("Review\rVote")),
                table(("Yes", 0, 0), ("Yes", 10, 0)),
                textbox(chars("Vote")),
            )
        },
        [(3, 256, outline_text(0, "Agenda") + outline_text(1, "Review\rVote"))],
    )
    assert text_of(stream, user) == "Agenda\nReview\nVote\nYes\tYes\nVote"


def test_table_rows_are_rebuilt_from_the_place_of_the_cells() -> None:
    stream, user = build(
        {
            3: slide(
                table(
                    ("Seats", 100, 0),
                    ("12", 100, 50),
                    ("Room", 0, 0),
                    ("Kalahari", 0, 50),
                    ("Two\rlines", 200, 50),
                )
            )
        },
        [(3, 256, [])],
    )
    assert text_of(stream, user) == "Room\tSeats\nKalahari\t12\tTwo lines"


def test_text_bytes_are_windows_1252_and_lines_are_normalised() -> None:
    """TextBytesAtom: Latin-1 by the format, cp1252 in practice for 0x80-0x9F (€, dashes);
    `\\r` and `\\r\\n` end a paragraph, 0x0B breaks a line, control characters go, a lone
    field ("*", the slide number) too."""
    text = (
        b"Caf\xe9 \x80 5 \x96 d\xe9j\xe0 vu\r\x93quoted\x94\x0bnext line\r\n\x01bell\x07\r*\r  \r"
    )
    stream, user = build(
        {3: slide(textbox(atom(BYTES, text)), textbox(chars("Zürich\x0b– tab\there\r")))},
        [(3, 256, [])],
    )
    assert text_of(stream, user) == (
        "Café € 5 – déjà vu\n“quoted”\nnext line\nbell\nZürich\n– tab\there"
    )


def test_fast_saved_file_uses_the_newest_version_of_each_slide() -> None:
    """A second edit appends a new version of the slide and a directory pointing to it; an edit
    that points back to itself does not loop."""
    stream, _ = build({3: slide(textbox(chars("Old text")))}, [(3, 256, [])])
    first_edit = len(stream) - len(user_edit(0))
    newer = len(stream)
    stream += slide(textbox(chars("New text")))
    directory = len(stream)
    stream += atom(DIRECTORY, struct.pack("<IIII", (1 << 20) | 1, 0, (1 << 20) | 3, newer))
    last = len(stream)
    stream += user_edit(directory, previous=first_edit)
    assert text_of(stream, current_user(last)) == "New text"
    looping = stream[:last] + user_edit(directory, previous=last)
    assert text_of(looping, current_user(last)) == "New text"


def test_current_user_pointing_elsewhere_falls_back_to_the_last_edit() -> None:
    stream, _ = build({3: slide(textbox(chars("Found anyway")))}, [(3, 256, [])])
    assert text_of(stream, current_user(5)) == "Found anyway"
    assert text_of(stream, None) == "Found anyway"
    assert text_of(stream, b"short") == "Found anyway"


def test_damaged_structure_is_read_in_the_order_of_the_stream() -> None:
    """No edit at all: the containers in the order of the stream, masters and their prompts
    left out, each paragraph once."""
    master = box(MAIN_MASTER, drawing(textbox(chars("Master only"))))
    title_master = slide(textbox(chars("Click to edit Master title style\rSecond level")))
    stream = (
        box(DOCUMENT, box(LIST, persist_atom(3, 256), *outline_text(0, "Kept title")))
        + master
        + title_master
        + slide(textbox(chars("Kept title")), textbox(chars("Kept body")))
        + notes(256, "Kept notes")
    )
    budget = Budget()
    assert text_of(stream, None, budget) == "Kept title\n\nKept body\n\nKept notes"
    assert budget.notes == ["PowerPoint structure damaged: text read in the order of the file"]


def test_lying_lengths_stop_the_reading_of_their_container_only() -> None:
    """A record longer than its container, or than the stream: neither it nor what follows in
    that container is read; the rest of the document is."""
    liar = struct.pack("<HHI", 0, CHARS, 0xFFFFFFF0) + "lost".encode("utf-16-le")
    stream, user = build(
        {
            3: slide(textbox(chars("Before"), liar, chars("After, lost too"))),
            4: slide(textbox(chars("Next slide"))),
        },
        [(3, 256, []), (4, 257, [])],
    )
    assert text_of(stream, user) == "Before\n\nNext slide"
    # A directory pointing out of the stream, or to a record that is not a slide.
    stream, user = build({3: slide(textbox(chars("Unreached")))}, [(3, 256, []), (9, 257, [])])
    assert text_of(stream, user) == "Unreached"


def test_deep_nesting_is_cut_without_recursion_error() -> None:
    inner = chars("Too deep")
    for _ in range(5000):
        inner = box(GROUP, inner)
    budget = Budget()
    stream, user = build({3: slide(textbox(chars("Shallow")), inner)}, [(3, 256, [])])
    assert text_of(stream, user, budget) == "Shallow"
    assert budget.notes == [f"records nested deeper than {extract_ppt.MAX_DEPTH}: skipped"]


def test_record_count_and_text_length_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    shapes = [textbox(chars(f"Paragraph {n}")) for n in range(200)]
    stream, user = build({3: slide(*shapes), 4: slide(textbox(chars("Second")))}, [
        (3, 256, []), (4, 257, [])
    ])  # fmt: skip
    monkeypatch.setattr(extract_ppt, "MAX_TEXT", 1000)
    budget = Budget()
    text = text_of(stream, user, budget)
    assert text == "" and budget.notes == ["text longer than 0 MB: truncated"]
    monkeypatch.setattr(extract_ppt, "MAX_TEXT", 20_000_000)
    monkeypatch.setattr(extract_ppt, "MAX_RECORDS", 500)
    budget = Budget()
    text_of(stream, user, budget)
    assert budget.notes == ["more than 500 records: the rest of the file is skipped"]


def test_slides_read_before_a_bound_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, user = build(
        {3: slide(textbox(chars("First"))), 4: slide(*[textbox(chars("x" * 50))] * 100)},
        [(3, 256, []), (4, 257, [])],
    )
    monkeypatch.setattr(extract_ppt, "MAX_TEXT", 2000)
    assert text_of(stream, user) == "First"


def test_the_deadline_stops_the_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, user = build({3: slide(textbox(chars("Late")))}, [(3, 256, [])])
    monkeypatch.setattr(extract_ppt, "CHECK_EVERY", 1)
    budget = Budget(timeout=0)
    assert text_of(stream, user, budget) == ""
    assert budget.notes == ["timeout reached: the rest of the file is skipped"]


def test_encrypted_documents_are_recognised() -> None:
    stream, user = build({3: slide(textbox(chars("Secret")))}, [(3, 256, [])])
    with pytest.raises(Encrypted):  # the token of Current User
        text_of(stream, current_user(0, token=0xF3D1C4DF))
    # The last edit points to an encryption session (CryptSession10Container).
    session = box(CRYPT, atom(0x2F15, bytes(16)))
    stream, user = build({3: slide(textbox(chars("Secret"))), 7: session}, [(3, 256, [])], crypt=7)
    with pytest.raises(Encrypted):
        text_of(stream, user)
    # No edit to follow, an encryption session in the stream.
    with pytest.raises(Encrypted):
        text_of(slide(textbox(chars("Secret"))) + session, None)
