# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "olefile==0.47",
#     "python-docx==1.2.0",
#     "python-pptx==1.0.2",
#     "xlsxwriter==3.2.9",
# ]
# ///
"""Generates the old binary Office files (.doc, .xls, .ppt) used by the tests and the demo.

Synaptane reads the .doc and .xls with catdoc (`catdoc`, `xls2csv`) and the .ppt with its own
reader (src/synaptane/extract_ppt.py). Real files are needed to test that: this script writes
small modern documents (.docx, .xlsx, .pptx), then has LibreOffice convert them in a
throwaway Debian container, so that nothing is installed on the machine. The files are kept
as LibreOffice writes them. They are committed; run this script again only to change them:

    uv run scripts/legacy_files.py [--image IMAGE]

`uv run` reads the dependencies above and installs them in a throwaway environment: they are
development tools of this script, not dependencies of Synaptane. Docker is required. The
container installs LibreOffice unless the image given with `--image` already has `soffice`.

Everything is fictional and the document properties carry no personal data: the author is
"Acme IT team", the dates are fixed; the script checks the properties of the converted files
and stops if they hold anything else. Each file holds a word found nowhere else ("witness"):

- tests/fixtures/legacy/printer-memo.doc: Zanzibar (tests/unit/test_extract_legacy.py)
- tests/fixtures/legacy/spare-parts.xls: Okavango
- tests/fixtures/legacy/kickoff-slides.ppt: Kilimanjaro (title), Serengeti (free text box),
  Kalahari (table), Zambezi (speaker notes)
- examples/acme-wiki/content/infra/documents/archive/hardware-inventory-2019.xls: NetWare
  (demo data set, tests/integration/test_demo.py)
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "legacy"
DEMO = ROOT / "examples" / "acme-wiki" / "content" / "infra" / "documents" / "archive"
AUTHOR = "Acme IT team"
WHEN = datetime(2026, 10, 1, 9, 0, 0)
NO_THUMBNAIL = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<item oor:path="/org.openoffice.Office.Common/Filter/Microsoft/Export">'
    '<prop oor:name="EnablePowerPointPreview" oor:op="fuse"><value>false</value></prop>'
    '<prop oor:name="EnableExcelPreview" oor:op="fuse"><value>false</value></prop></item>'
    "</oor:items>"
)
PACKAGES = "libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui"


def _properties(props: object, title: str) -> None:
    props.author = props.last_modified_by = AUTHOR  # type: ignore[attr-defined]
    props.title = title  # type: ignore[attr-defined]
    props.subject = props.keywords = props.comments = props.category = ""  # type: ignore[attr-defined]
    props.created = props.modified = props.last_printed = WHEN  # type: ignore[attr-defined]
    props.revision = 1  # type: ignore[attr-defined]


# --- Word: printer maintenance memo -----------------------------------------------------------


def printer_memo(target: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("Printer maintenance memo", level=0)
    document.add_paragraph(
        "The Zanzibar colour printer on the second floor gets a new fuser unit on Monday. "
        "Print jobs go to the café printer in the meantime."
    )
    document.add_heading("Schedule", level=1)
    table = document.add_table(rows=1, cols=3)
    for cell, text in zip(table.rows[0].cells, ("Day", "Task", "Cost"), strict=True):
        cell.text = text
    for row in (
        ("Monday", "Fuser unit replaced – 2 hours", "€ 180"),
        ("Tuesday", "Test pages, Zürich office template", "€ 0"),
    ):
        cells = table.add_row().cells
        for cell, text in zip(cells, row, strict=True):
            cell.text = text
    document.add_paragraph("Questions: the Acme IT team, extension 4242.")
    _properties(document.core_properties, "Printer maintenance memo")
    document.save(str(target))


# --- Excel: spare parts, and the 2019 hardware inventory of the demo ---------------------------


SPARE_PARTS = [
    ("Part", "Quantity", "Unit price (€)", "Cabinet", "Last checked"),
    ("Toner, black", 6, 54.9, "Okavango", "2026-09-14"),
    ('Patch cable "Cat 6", 2 m', 40, 3.5, "Okavango", "2026-09-14"),
    ("Fuser unit", 1, 180, "Basement", "2026-08-03"),
]
SUPPLIERS = [("Supplier", "Website"), ("Brightrack", "brightrack.example")]

INVENTORY_2019 = """
Asset tag | Type | Name | Model | Location | Purchased | Status
HW-0007 | Server | files1 | Novell NetWare 6.5 file server | Rack A | 2009-04-20 | Retired 2019
HW-0011 | Server | mail1 | Tower server T110 | Rack A | 2011-06-01 | Retired 2019
HW-0024 | Server | web0 | Rack server R210 | Rack A | 2013-02-11 | Replaced by web1
HW-0031 | Switch | sw-old | Switch 24p 100 Mbit/s | Rack A | 2010-09-15 | Replaced by core-a
HW-0045 | Desktop | pc-0045 | Desktop SFF | Office | 2014-03-03 | Recycled
HW-0046 | Desktop | pc-0046 | Desktop SFF | Office | 2014-03-03 | Recycled
"""


def spreadsheet(target: Path, title: str, sheets: dict[str, list[tuple[object, ...]]]) -> None:
    """Dates are written as text: LibreOffice saves every number format in the .xls, built-in
    ones included, and xls2csv then only sees numbers (days since 1900) in date cells."""
    import xlsxwriter

    book = xlsxwriter.Workbook(str(target))
    book.set_properties({"title": title, "author": AUTHOR, "company": "Acme", "created": WHEN})
    bold = book.add_format({"bold": True})
    for name, rows in sheets.items():
        sheet = book.add_worksheet(name)
        sheet.write_row(0, 0, rows[0], bold)
        for number, row in enumerate(rows[1:], start=1):
            sheet.write_row(number, 0, row)
        sheet.set_column(0, len(rows[0]) - 1, 16)
    book.close()


def inventory_rows() -> list[tuple[object, ...]]:
    return [tuple(line.split(" | ")) for line in INVENTORY_2019.strip().splitlines()]


# --- PowerPoint: project kickoff ---------------------------------------------------------------


def kickoff_slides(target: Path) -> None:
    """Title and body placeholders, a free text box, a table and speaker notes: LibreOffice
    writes them all in the drawing of the slides (none in the outline, unlike PowerPoint)."""
    from pptx import Presentation
    from pptx.util import Cm

    deck = Presentation()
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = "Project Kilimanjaro kickoff"
    cover.placeholders[1].text = "Acme IT team · new meeting rooms"
    slides = []
    for heading, points in (
        ("Goals", ["Video conferencing in the four meeting rooms", "One remote for everything"]),
        ("Budget", ["Screens and cameras: € 12,000", "Cabling: done by the Acme IT team"]),
    ):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = heading
        frame = slide.placeholders[1].text_frame
        frame.text = points[0]
        for point in points[1:]:
            frame.add_paragraph().text = point
        slides.append(slide)
    box = slides[0].shapes.add_textbox(Cm(2), Cm(15), Cm(20), Cm(2)).text_frame
    box.text = "Pilot room: Serengeti, third floor"
    notes = slides[1].notes_slide.notes_text_frame
    notes.text = "Ask the Zambezi supplier for a second quote before Friday."
    rooms = deck.slides.add_slide(deck.slide_layouts[5])  # title only
    rooms.shapes.title.text = "Rooms"
    table = rooms.shapes.add_table(3, 3, Cm(2), Cm(5), Cm(20), Cm(4)).table
    for row, cells in enumerate(
        (("Room", "Seats", "Screen"), ("Kalahari", "12", "75 inch"), ("Okapi", "6", "55 inch"))
    ):
        for column, text in enumerate(cells):
            table.cell(row, column).text = text
    _properties(deck.core_properties, "Project Kilimanjaro kickoff")
    deck.save(str(target))


# --- Conversion and checks ---------------------------------------------------------------------


def convert(folder: Path, image: str) -> None:
    """LibreOffice, in a throwaway container: .docx → .doc, .xlsx → .xls, .pptx → .ppt."""
    script = (
        "set -e; export HOME=/tmp;"
        " if ! command -v soffice >/dev/null; then"
        "  apt-get update -qq"
        f" && apt-get install -y -qq --no-install-recommends {PACKAGES} >/dev/null;"
        " fi;"
        # No thumbnail in the properties (half a megabyte in a .ppt).
        " mkdir -p /tmp/.config/libreoffice/4/user;"
        f" printf '%s' '{NO_THUMBNAIL}'"
        " > /tmp/.config/libreoffice/4/user/registrymodifications.xcu;"
        " cd /work;"
        " soffice --headless --convert-to doc:'MS Word 97' *.docx >/dev/null 2>&1;"
        " soffice --headless --convert-to xls:'MS Excel 97' *.xlsx >/dev/null 2>&1;"
        " soffice --headless --convert-to ppt:'MS PowerPoint 97' *.pptx >/dev/null 2>&1;"
        ' chown "$OWNER" *.doc *.xls *.ppt'
    )
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-e", f"OWNER={os.getuid()}:{os.getgid()}",
            "-v", f"{folder}:/work",
            image, "sh", "-c", script,
        ],
        check=True,
    )  # fmt: skip


def check_properties(path: Path) -> None:
    """The converted file says nothing but the author and the fixed dates; stop otherwise."""
    import olefile

    with olefile.OleFileIO(str(path)) as ole:
        meta = ole.get_metadata()
    people = {meta.author, meta.last_saved_by}
    people.discard(None)
    people.discard(b"")
    if people - {AUTHOR.encode()}:
        sys.exit(f"{path.name}: unexpected people in the properties: {people}")
    for field in ("company", "manager"):
        value = getattr(meta, field)
        if value not in (None, b"", b"Acme"):
            sys.exit(f"{path.name}: unexpected {field}: {value!r}")
    for field in ("create_time", "last_saved_time", "last_printed"):
        value = getattr(meta, field)
        if value is not None and value.year > 1601 and value.date() != WHEN.date():
            print(f"  {path.name}: {field} = {value} (conversion date)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="debian:trixie-slim", help="Docker image to use")
    args = parser.parse_args()
    if shutil.which("docker") is None:
        sys.exit("docker is required")
    with tempfile.TemporaryDirectory(prefix="synaptane-legacy-") as scratch:
        work = Path(scratch)
        printer_memo(work / "printer-memo.docx")
        spreadsheet(
            work / "spare-parts.xlsx",
            "Spare parts",
            {"Spare parts": SPARE_PARTS, "Suppliers": SUPPLIERS},
        )
        spreadsheet(
            work / "hardware-inventory-2019.xlsx",
            "Hardware inventory 2019",
            {"Hardware 2019": inventory_rows()},
        )
        kickoff_slides(work / "kickoff-slides.pptx")
        convert(work, args.image)
        FIXTURES.mkdir(parents=True, exist_ok=True)
        DEMO.mkdir(parents=True, exist_ok=True)
        targets = {
            "printer-memo.doc": FIXTURES,
            "spare-parts.xls": FIXTURES,
            "kickoff-slides.ppt": FIXTURES,
            "hardware-inventory-2019.xls": DEMO,
        }
        for name, folder in targets.items():
            check_properties(work / name)
            shutil.copyfile(work / name, folder / name)
            path = folder / name
            print(f"{path.relative_to(ROOT)}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
