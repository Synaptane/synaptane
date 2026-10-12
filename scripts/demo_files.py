# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pillow==12.3.0",
#     "python-docx==1.2.0",
#     "python-pptx==1.0.2",
#     "reportlab==5.0.1",
#     "xlsxwriter==3.2.9",
# ]
# ///
"""Generates the sample documents of the demo data set (`examples/acme-wiki/content/`).

The demo shows that Synaptane is not only Markdown: a text PDF, a scanned PDF (an image of a
page, no text layer: read by OCR), a Word document, a slide deck, a spreadsheet, an image with
text and a standalone HTML page. They are committed; run this script again only to change them:

    uv run scripts/demo_files.py

`uv run` reads the dependencies above and installs them in a throwaway environment: they are
development tools of this script, not dependencies of Synaptane. Everything is fictional (the
Acme IT team, reserved domains only) and the document properties carry no personal data: the
author is "Acme IT team", dates are fixed. Office files are rewritten with fixed zip timestamps,
so that running the script twice gives the same bytes.

Each document holds a word found nowhere else in the data set ("witness"), checked by
`tests/integration/test_demo.py`: Lindqvist (contract), Kestrel (scanned delivery note, OCR),
lanyard (checklist), MTTR (slides), docking (inventory), Annapurna (rack image, OCR), uptime
(status report); and NetWare for the 2019 inventory in the old .xls format, made by
`scripts/legacy_files.py`. Keep them unique when editing the data set.
"""

import io
import random
import re
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "examples" / "acme-wiki" / "content"
AUTHOR = "Acme IT team"
WHEN = datetime(2026, 10, 1, 9, 0, 0)
ZIP_TIME = (2026, 10, 1, 9, 0, 0)

# --- Text PDF: hardware support contract ----------------------------------------------------


def support_contract(target: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    body, title, heading = styles["BodyText"], styles["Title"], styles["Heading2"]
    grid = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef4")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )

    def table(rows: list[list[str]], widths: list[float]) -> Table:
        cells = [[Paragraph(cell, body) for cell in row] for row in rows]
        return Table(cells, colWidths=[w * cm for w in widths], style=grid, hAlign="LEFT")

    story = [
        Paragraph("Hardware support contract", title),
        Paragraph(
            "Contract no. BRS-2025-0187 &middot; between <b>Brightrack Services</b> "
            "(the provider) and <b>Acme</b>, IT team (the customer).",
            body,
        ),
        Spacer(1, 0.4 * cm),
        Paragraph("1. Term", heading),
        Paragraph(
            "From 1 March 2025 to 28 February 2027. The contract renews for one year unless "
            "either party gives three months' written notice.",
            body,
        ),
        Paragraph("2. Service level", heading),
        Paragraph(
            "Next-business-day on-site intervention for the equipment listed in schedule A. "
            "Cases are opened on the provider's portal, support.brightrack.example, or by "
            "e-mail to support@brightrack.example, quoting the contract number and the serial "
            "number of the machine.",
            body,
        ),
        Spacer(1, 0.2 * cm),
        table(
            [
                ["Priority", "Example", "Response", "On site"],
                ["P1", "Server down, service stopped", "2 hours", "Next business day"],
                [
                    "P2",
                    "Redundant part failed (disk, power supply)",
                    "4 hours",
                    "Next business day",
                ],
                ["P3", "Question, configuration advice", "1 business day", "Not included"],
            ],
            [2, 7, 3, 4],
        ),
        Paragraph("3. Spare parts", heading),
        Paragraph(
            "The provider keeps a stock of disks, power supplies and memory modules for the "
            "covered models. Replaced disks stay with the customer, who destroys them: no "
            "data leaves Acme.",
            body,
        ),
        Paragraph("4. Exclusions", heading),
        Paragraph(
            "Software, operating systems and virtual machines; damage caused by a power "
            "failure outside the provider's equipment; consumables.",
            body,
        ),
        Paragraph("5. Price", heading),
        Paragraph(
            "4,800 EUR excluding VAT per year, invoiced in March. Equipment added during the "
            "year is invoiced pro rata.",
            body,
        ),
        Paragraph("6. Contacts", heading),
        table(
            [
                ["Role", "Name", "Address"],
                ["Customer, contract owner", "Claire Martin", "claire.martin@acme.example"],
                ["Customer, technical contact", "Hugo Bernard", "hugo.bernard@acme.example"],
                ["Provider, account manager", "Jonas Lindqvist", "accounts@brightrack.example"],
            ],
            [5, 4, 7],
        ),
        Spacer(1, 0.6 * cm),
        Paragraph(
            "Signed on 14 February 2025. For Acme: Claire Martin, IT team lead. For Brightrack "
            "Services: Jonas Lindqvist, account manager.",
            body,
        ),
        PageBreak(),
        Paragraph("Schedule A: covered equipment", heading),
        table(
            [
                ["Name", "Role", "Location", "Serial number"],
                ["web1", "public website", "rack A", "BRX-7731-0042"],
                ["web2", "public website (standby)", "rack A", "BRX-7731-0043"],
                ["db1", "primary PostgreSQL", "rack B", "BRX-9120-0108"],
                ["db2", "PostgreSQL read replica", "rack B", "BRX-9120-0109"],
                ["pve1", "Proxmox hypervisor", "rack B", "BRX-9340-0021"],
            ],
            [3, 6, 3, 4],
        ),
        Spacer(1, 0.4 * cm),
        Paragraph(
            "Laptops and network equipment are not covered by this contract: laptops come "
            "with the manufacturer's warranty, switches and firewalls with their own support.",
            body,
        ),
    ]
    SimpleDocTemplate(
        str(target),
        pagesize=A4,
        title="Hardware support contract BRS-2025-0187",
        author=AUTHOR,
        subject="Brightrack Services hardware support",
        creator=AUTHOR,
        producer=AUTHOR,
        invariant=True,  # fixed dates and document ID
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    ).build(story)


# --- Scanned PDF: delivery note, an image without a text layer ------------------------------


def delivery_note(target: Path) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    dpi = 150
    width, height = int(8.27 * dpi), int(11.69 * dpi)  # A4
    page = Image.new("L", (width, height), 246)
    draw = ImageDraw.Draw(page)

    def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        return ImageFont.load_default(size=size)  # bundled with Pillow: same on every machine

    left, y = 110, 120
    draw.text((left, y), "BRIGHTRACK SERVICES", fill=20, font=font(44))
    y += 64
    draw.text((left, y), "Delivery note DN-26-0912", fill=30, font=font(30))
    y += 70
    lines = [
        "Date: 23 September 2026",
        "Order: PO-2026-041 (Acme, IT team)",
        "Carrier: Kestrel Freight, 2 parcels",
        "Deliver to: Acme, IT team, attn. Malik Robert",
    ]
    for line in lines:
        draw.text((left, y), line, fill=35, font=font(26))
        y += 42
    y += 30
    draw.line((left, y, width - left, y), fill=60, width=3)
    y += 20
    columns = (left, left + 110, left + 760)
    for x, header in zip(columns, ("Qty", "Item", "Asset tags"), strict=True):
        draw.text((x, y), header, fill=20, font=font(26))
    y += 46
    items = [
        ("4", "Laptop 14 inch, 32 GB RAM", "HW-0201 to HW-0204"),
        ("4", "USB-C power supply 65 W", "-"),
        ("4", "Laptop sleeve", "-"),
    ]
    for quantity, item, tags in items:
        for x, cell in zip(columns, (quantity, item, tags), strict=True):
            draw.text((x, y), cell, fill=35, font=font(26))
        y += 44
    draw.line((left, y + 10, width - left, y + 10), fill=60, width=3)
    y += 70
    draw.text((left, y), "Received in good condition, all items checked.", fill=35, font=font(26))
    y += 60
    draw.text((left, y), "Signature: M. Robert, 23/09", fill=50, font=font(30))
    # A little of what a scanner adds: specks, a slight blur and a skew.
    specks = random.Random(912)
    for _ in range(400):
        x, y = specks.randrange(width), specks.randrange(height)
        draw.point((x, y), fill=specks.randrange(120, 200))
    scan = page.filter(ImageFilter.GaussianBlur(0.6)).rotate(
        0.7, resample=Image.Resampling.BICUBIC, fillcolor=246
    )
    scan.save(
        target,
        "PDF",
        resolution=dpi,
        quality=70,
        title="Delivery note DN-26-0912",
        author=AUTHOR,
        creator=AUTHOR,
        producer=AUTHOR,
        creationDate=WHEN.timetuple(),
        modDate=WHEN.timetuple(),
    )


# --- Word document: onboarding checklist ----------------------------------------------------


def onboarding_checklist(target: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("New employee onboarding checklist", level=0)
    document.add_paragraph(
        "Print this checklist for each newcomer and tick the boxes as you go. The wiki page "
        "team/onboarding.md explains each step; the team lead keeps the completed checklist."
    )
    steps = [
        ("Before day one", [
            ("Order and prepare the laptop (procedure new-laptop)", "Malik Robert"),
            ("Create the account and the firstname.lastname@acme.example mailbox", "Malik Robert"),
            ("Add the newcomer to the team chat channels", "Claire Martin"),
        ]),
        ("Day one", [
            ("Collect the access badge and lanyard at reception", "Newcomer"),
            ("Hand over the laptop, check that full-disk encryption is on", "Malik Robert"),
            ("Send the VPN profile and test the connection from home", "Inès Petit"),
            ("Add them to their team's collection in the password vault", "Hugo Bernard"),
        ]),
        ("First week", [
            ("Security awareness training", "Inès Petit"),
            ("Read the glossary, the FAQ and the on-call page of the wiki", "Newcomer"),
            ("Shadow the person on call for one day", "On-call"),
        ]),
        ("End of the first month", [
            ("Review with the team lead: access, questions, first tasks", "Claire Martin"),
        ]),
    ]  # fmt: skip
    for section, rows in steps:
        document.add_heading(section, level=1)
        table = document.add_table(rows=1, cols=3)
        table.style = "Light Grid Accent 1"
        for cell, text in zip(table.rows[0].cells, ("Done", "Step", "Who"), strict=True):
            cell.text = text
        for step, who in rows:
            cells = table.add_row().cells
            cells[0].text, cells[1].text, cells[2].text = "☐", step, who
    document.add_paragraph()
    document.add_paragraph("Newcomer: ____________________    Start date: ____________")
    props = document.core_properties
    props.author = props.last_modified_by = AUTHOR
    props.title = "New employee onboarding checklist"
    props.subject = props.keywords = props.comments = props.category = ""
    props.created = props.modified = WHEN
    props.last_printed = WHEN
    props.revision = 1
    document.save(str(target))


# --- Slides: quarterly infrastructure review -------------------------------------------------


def quarterly_review(target: Path) -> None:
    from pptx import Presentation
    from pptx.util import Pt

    deck = Presentation()
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = "Infrastructure review: Q3 2026"
    cover.placeholders[1].text = "Acme IT team · quarterly review, 6 October 2026"
    slides = [
        ("Availability", [
            "Public website (web1, web2): available 99.97 % of the time",
            "Primary database (db1): 99.99 %, one planned restart",
            "Incidents: no P1, two P2 (VPN certificate, full log partition)",
            "MTTR: 1 h 40 min (Q2: 2 h 15 min)",
        ]),
        ("What we delivered", [
            "Prometheus pilot on three servers, next to Zabbix",
            "Automatic renewal of the internal certificates",
            "Four laptops delivered and prepared for the September arrivals",
            "Restore test of db1 from the night's backup: 38 minutes",
        ]),
        ("Risks", [
            "pve1 is our only hypervisor: no failover if it stops",
            "db1 disk usage at 78 % (alert threshold 85 %)",
            "Firewall page overdue for review in the wiki",
        ]),
        ("Q4 priorities", [
            "Decide on the Ceph project (owner: Hugo Bernard)",
            "Prometheus in production for all servers (owner: Inès Petit)",
            "Quarterly restore test, documented (owner: Malik Robert)",
            "Next review: January 2027 (owner: Claire Martin)",
        ]),
    ]  # fmt: skip
    for heading, points in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = heading
        frame = slide.placeholders[1].text_frame
        frame.text = points[0]
        for point in points[1:]:
            frame.add_paragraph().text = point
        for paragraph in frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(24)
    props = deck.core_properties
    props.author = props.last_modified_by = AUTHOR
    props.title = "Infrastructure review: Q3 2026"
    props.subject = props.keywords = props.comments = props.category = ""
    props.created = props.modified = WHEN
    props.last_printed = WHEN
    props.revision = 1
    deck.save(str(target))


# --- Spreadsheet: hardware inventory --------------------------------------------------------


INVENTORY = """
Asset tag | Type | Name | Model | Location | Assigned to | Purchased | Warranty until | Notes
HW-0101 | Server | web1 | Brightrack R220 | Rack A | IT team | 2023-03-06 | 2027-02-28 | Website
HW-0102 | Server | web2 | Brightrack R220 | Rack A | IT team | 2023-03-06 | 2027-02-28 | Standby
HW-0103 | Server | db1 | Brightrack R440 | Rack B | IT team | 2024-01-15 | 2027-02-28 | PostgreSQL
HW-0104 | Server | db2 | Brightrack R440 | Rack B | IT team | 2024-01-15 | 2027-02-28 | Replica
HW-0105 | Server | pve1 | Brightrack R640 | Rack B | IT team | 2024-09-02 | 2027-02-28 | Proxmox
HW-0110 | Storage | nas | Brightrack NS8 | Rack B | IT team | 2022-11-21 | 2026-11-20 | Ends soon
HW-0120 | Switch | core-a | Core switch 48p | Rack A | IT team | 2022-05-09 | 2027-05-08 | Core
HW-0121 | Switch | core-b | Core switch 48p | Rack B | IT team | 2022-05-09 | 2027-05-08 | Core
HW-0201 | Laptop | lt-0201 | Laptop 14 | Office | Spare | 2026-09-23 | 2029-09-22 | DN-26-0912
HW-0202 | Laptop | lt-0202 | Laptop 14 | Office | Spare | 2026-09-23 | 2029-09-22 | DN-26-0912
HW-0203 | Laptop | lt-0203 | Laptop 14 | Office | M. Robert | 2026-09-23 | 2029-09-22 | DN-26-0912
HW-0204 | Laptop | lt-0204 | Laptop 14 | Office | I. Petit | 2026-09-23 | 2029-09-22 | DN-26-0912
HW-0301 | Docking station | - | USB-C dock | Office | C. Martin | 2025-02-10 | 2027-02-09 | -
HW-0302 | Docking station | - | USB-C dock | Office | H. Bernard | 2025-02-10 | 2027-02-09 | -
"""


def hardware_inventory(target: Path) -> None:
    """XlsxWriter rather than openpyxl: like Excel, it stores the text of the cells in the
    shared strings table, which is what Synaptane reads (openpyxl writes them inline)."""
    from datetime import date

    import xlsxwriter

    book = xlsxwriter.Workbook(str(target))
    book.set_properties(
        {"title": "Hardware inventory", "author": AUTHOR, "company": "Acme", "created": WHEN}
    )
    sheet = book.add_worksheet("Hardware")
    bold = book.add_format({"bold": True, "bg_color": "#E8EEF4"})
    day = book.add_format({"num_format": "yyyy-mm-dd"})
    lines = [line.split(" | ") for line in INVENTORY.strip().splitlines()]
    header, rows = lines[0], lines[1:]
    sheet.write_row(0, 0, header, bold)
    for number, row in enumerate(rows, start=1):
        sheet.write_row(number, 0, row[:6])
        for column in (6, 7):  # purchase and warranty: real dates in the sheet
            sheet.write_datetime(number, column, date.fromisoformat(row[column]), day)
        sheet.write_string(number, 8, row[8])
    for column, width in enumerate([11, 16, 9, 18, 9, 14, 12, 15, 14]):
        sheet.set_column(column, column, width)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(rows), len(header) - 1)
    book.close()


# --- Image with text: rack elevation ---------------------------------------------------------


def rack_elevation(target: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 900, 1100
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        return ImageFont.load_default(size=size)

    draw.text((60, 40), "RACK B ELEVATION (front view)", fill="black", font=font(40))
    draw.text(
        (60, 96),
        "Data room Annapurna, row 2. Updated September 2026.",
        fill="#333333",
        font=font(24),
    )
    top, unit, units = 160, 36, 24
    frame = (150, top, 750, top + unit * units)
    draw.rectangle(frame, outline="black", width=4)
    devices = [  # (top unit, height in units, label, fill)
        (24, 1, "Patch panel PP-B1", "#dfe6ee"),
        (23, 1, "core-b switch, 48 ports", "#cfe3d4"),
        (21, 2, "db1, primary PostgreSQL", "#f3e1c7"),
        (19, 2, "db2, PostgreSQL read replica", "#f3e1c7"),
        (16, 2, "pve1, Proxmox hypervisor", "#e3d6ef"),
        (11, 4, "NAS, nas.acme.internal", "#d6e4f0"),
        (2, 1, "PDU B1", "#eeeeee"),
        (1, 1, "PDU B2", "#eeeeee"),
    ]
    for first, size, label, fill in devices:
        y0 = top + (units - first) * unit
        y1 = y0 + size * unit
        draw.rectangle((154, y0 + 2, 746, y1 - 2), fill=fill, outline="#555555", width=2)
        draw.text((180, (y0 + y1) / 2 - 14), label, fill="black", font=font(26))
    for number in range(1, units + 1):
        y = top + (units - number) * unit + 6
        draw.text((100, y), f"U{number}", fill="#444444", font=font(20))
    draw.text(
        (60, top + unit * units + 40),
        "Free: U3 to U7, U12 to U14, U17, U22.",
        fill="black",
        font=font(26),
    )
    image.quantize(colors=32).save(target, optimize=True)


# --- Standalone HTML page: weekly status report ---------------------------------------------

STATUS_REPORT = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly status report, week 40</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; color: #1d2733; background: #fafbfc; }
  h1 { font-size: 1.5rem; margin-bottom: 0.2rem; }
  .period { color: #5a6b7d; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; max-width: 46rem; margin: 1rem 0 2rem; }
  th, td { text-align: left; padding: 0.45rem 0.7rem; border-bottom: 1px solid #dde3ea; }
  th { background: #e8eef4; }
  .ok { color: #1f7a3a; font-weight: 600; }
  .warn { color: #a05a00; font-weight: 600; }
  .bar { background: #e8eef4; width: 12rem; height: 0.8rem; border-radius: 0.4rem; }
  .bar span { display: block; height: 100%; border-radius: 0.4rem; background: #3d7bb8; }
  .bar .high { background: #d08a1f; }
  footer { color: #5a6b7d; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Weekly status report, week 40</h1>
<p class="period">29 September to 5 October 2026 &middot; Acme IT team &middot; exported from
the Zabbix dashboard</p>

<h2>Services</h2>
<table>
  <tr><th>Service</th><th>Hosts</th><th>Uptime</th><th>State</th></tr>
  <tr><td>Public website</td><td>web1, web2</td><td>100 %</td><td class="ok">OK</td></tr>
  <tr><td>PostgreSQL</td><td>db1, db2</td><td>99.99 %</td><td class="ok">OK</td></tr>
  <tr><td>VPN</td><td>vpn.acme.internal</td><td>99.95 %</td><td class="ok">OK</td></tr>
  <tr><td>GitLab</td><td>gitlab.acme.internal</td><td>99.90 %</td>
    <td class="warn">Slow on 2 October</td></tr>
  <tr><td>Tickets</td><td>glpi.acme.internal</td><td>100 %</td><td class="ok">OK</td></tr>
</table>

<h2>Disk usage</h2>
<table>
  <tr><th>Host</th><th>Used</th><th></th></tr>
  <tr><td>db1</td><td>78 %</td><td><div class="bar"><span class="high" style="width: 78%">
    </span></div></td></tr>
  <tr><td>pve1</td><td>64 %</td><td><div class="bar"><span style="width: 64%"></span></div>
    </td></tr>
  <tr><td>web1</td><td>41 %</td><td><div class="bar"><span style="width: 41%"></span></div>
    </td></tr>
</table>

<h2>Alerts this week</h2>
<table>
  <tr><th>When</th><th>Host</th><th>Alert</th><th>Resolved</th></tr>
  <tr><td>30 Sep, 02:14</td><td>gitlab</td><td>CI runner queue above 20 jobs</td>
    <td>02:51</td></tr>
  <tr><td>2 Oct, 11:05</td><td>gitlab</td><td>HTTP response time above 2 s</td>
    <td>11:40</td></tr>
  <tr><td>4 Oct, 23:30</td><td>db1</td><td>Disk usage above 75 % (warning)</td>
    <td>open</td></tr>
</table>

<footer>Generated every Monday at 08:00 for the team meeting. Alert threshold for disks: 85 %.
On call this week: Hugo Bernard.</footer>
</body>
</html>
"""


# --- Common -----------------------------------------------------------------------------------


def fixed_zip(path: Path) -> None:
    """Rewrites an Office file (a zip) with fixed timestamps, and a fixed modification date
    in its properties: reproducible bytes."""
    with zipfile.ZipFile(path) as source:
        parts = [(info.filename, source.read(info)) for info in source.infolist()]
    stamp = WHEN.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    parts = [
        (name, re.sub(rb"(<dcterms:modified[^>]*>)[^<]*", rb"\g<1>" + stamp, data))
        if name == "docProps/core.xml"
        else (name, data)
        for name, data in parts
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts:
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data)
    path.write_bytes(buffer.getvalue())


def main() -> None:
    infra = ROOT / "infra" / "documents"
    team = ROOT / "team" / "documents"
    infra.mkdir(parents=True, exist_ok=True)
    team.mkdir(parents=True, exist_ok=True)
    support_contract(infra / "brightrack-support-contract.pdf")
    delivery_note(infra / "2026-09-delivery-note.pdf")
    hardware_inventory(infra / "hardware-inventory.xlsx")
    rack_elevation(infra / "rack-b-elevation.png")
    (infra / "status-report-2026-w40.html").write_text(STATUS_REPORT, encoding="utf-8")
    onboarding_checklist(team / "onboarding-checklist.docx")
    quarterly_review(team / "2026-q3-infrastructure-review.pptx")
    for office in (*team.glob("*.docx"), *team.glob("*.pptx")):  # XlsxWriter: already fixed
        fixed_zip(office)
    for path in sorted((*infra.iterdir(), *team.iterdir())):
        print(f"{path.relative_to(ROOT)}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
