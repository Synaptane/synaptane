"""Documents are read in a bounded child process.

Tesseract is replaced by a fake one, written in Python, put first in the PATH: it reports
what it received (image dimensions, environment) or misbehaves (memory, delay)."""

import math
import os
import resource
import struct
import sys
import time
import zlib
from pathlib import Path

import pytest

from synaptane import extract
from synaptane.extract import (
    MAX_PIXELS,
    NO_OCR_IMAGES,
    OCR_DPI,
    Budget,
    ExtractionFailed,
    extract_direct,
    page_scale,
    run_isolated,
)


def blank_pdf(side: int, pages: int = 1) -> bytes:
    """Tiny PDF (a few hundred bytes): `pages` pages without text, with a `MediaBox` of
    `side` points. At 4,500 pt, rendering at 300 dpi asked for 18,750 × 18,750 px."""
    kids = " ".join(f"{3 + n} 0 R" for n in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        *[f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {side} {side}] >>".encode()] * pages,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def png_header(width: int, height: int) -> bytes:
    """Valid PNG header announcing a huge image, without any image data."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


REPORT_SIZE = """
import struct, sys
with open(sys.argv[1], "rb") as image:
    width, height = struct.unpack(">II", image.read(24)[16:24])
print(f"Texte reconnu sur une image de {width} x {height} pixels.")
"""


def fake_tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    script = folder / "tesseract"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


def pixels(width: float, height: float) -> int:
    """Size of the rendering asked from pdfium (which rounds up to the next pixel)."""
    scale = page_scale(width, height)
    return math.ceil(width * scale) * math.ceil(height * scale)


def test_page_scale_caps_pixels_before_rendering() -> None:
    assert page_scale(595, 842) == OCR_DPI / 72  # A4: 300 dpi, under the cap
    assert pixels(4500, 4500) <= MAX_PIXELS < pixels(4500, 4500) * 1.01
    assert pixels(14400, 1) <= MAX_PIXELS and pixels(1, 14400) <= MAX_PIXELS
    assert page_scale(0, 842) == 0 and page_scale(float("inf"), 1) == 0


def test_giant_pdf_stays_within_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(tmp_path, monkeypatch, REPORT_SIZE)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    data = blank_pdf(4500)
    assert len(data) < 500
    result = run_isolated("pdf", ".pdf", data, languages="fra", ocr_images=True)
    assert result.text is not None and "Texte reconnu" in result.text
    width, height = (
        int(n) for n in result.text.split("image de ")[1].split(" pixels")[0].split(" x ")
    )
    assert width * height <= MAX_PIXELS and width > 5000  # rendered, but at 40 Mpx at most
    # The worker read nothing itself; the child stayed far from the 2.4 GB measured before.
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 50 * 1024
    assert resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss < 512 * 1024


def test_tesseract_inherits_the_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runaway OCR dies under the child's limit; the page is lost, not the document, and
    the caller does not suffer from it."""
    fake_tesseract(tmp_path, monkeypatch, "block = bytearray(3 * 1024 ** 3)\nprint('jamais')\n")
    result = run_isolated("pdf", ".pdf", blank_pdf(600), languages="fra", ocr_images=True)
    assert result.text is not None and not result.text.strip()
    assert any("OCR failed" in note for note in result.notes), result.notes


def test_ocr_stops_at_the_document_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tesseract(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    start = time.monotonic()
    result = run_isolated(
        "pdf", ".pdf", blank_pdf(600, pages=3), languages="fra", ocr_images=True, timeout=3
    )
    assert time.monotonic() - start < 15
    assert result.text is not None and not result.text.strip()
    assert any("3 s timeout" in note for note in result.notes), result.notes


def test_a_stuck_child_is_killed_with_its_tesseract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "tesseract.pid"
    fake_tesseract(
        tmp_path,
        monkeypatch,
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n",
    )
    monkeypatch.setattr(extract, "KILL_MARGIN", -3)  # the worker only waits 2 s out of 5
    with pytest.raises(ExtractionFailed, match="timed out"):
        run_isolated("pdf", ".pdf", blank_pdf(600), languages="fra", ocr_images=True, timeout=5)
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:  # gone (FileNotFoundError), or reaped while being read (ESRCH)
            break
        if "\nState:\tZ" in status:
            break
        time.sleep(0.1)
    else:
        pytest.fail("le faux Tesseract tourne encore")


def test_child_sees_no_worker_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tesseract(
        tmp_path, monkeypatch, "import os\nprint('Variables : ' + ' '.join(os.environ))\n"
    )
    monkeypatch.setenv("SYNAPTANE_DATABASE_URL", "postgresql://secret@db/x")
    result = run_isolated("pdf", ".pdf", blank_pdf(600), languages="fra", ocr_images=True)
    assert result.text and "PATH" in result.text
    assert "SYNAPTANE" not in result.text and "HOME" not in result.text


def test_huge_image_is_refused_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tesseract(tmp_path, monkeypatch, REPORT_SIZE)
    with pytest.raises(ExtractionFailed, match="image too large"):
        run_isolated("image", ".png", png_header(30_000, 30_000), languages="fra", ocr_images=True)


def test_unreadable_document_is_a_failure_not_a_crash() -> None:
    with pytest.raises(ExtractionFailed) as failure:
        run_isolated("pdf", ".pdf", b"%PDF-1.4 tronque", languages="fra", ocr_images=False)
    assert str(failure.value)
    assert run_isolated("image", ".png", b"x", languages="fra", ocr_images=False).text is None


def _xlsx(sheet: str, shared: str | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>",
        )
        if shared is not None:
            archive.writestr("xl/sharedStrings.xml", f"<sst {ns}>{shared}</sst>")
    return buffer.getvalue()


def test_xlsx_inline_strings_are_extracted() -> None:
    """openpyxl and pandas write the cell text inline in the sheet, not in shared strings."""
    sheet = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Docking station</t></is></c>'
        '<c r="B1"><v>12</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="str"><f>A1</f><v>Rack B</v></c></row>'
    )
    text = extract.extract_office(_xlsx(sheet, "<si><t>Monitor</t></si>"), ".xlsx")
    assert text.splitlines() == ["Monitor", "Docking station\t12", "Rack B"]  # no index "0"


@pytest.mark.parametrize("suffix", sorted(NO_OCR_IMAGES))
def test_images_tesseract_cannot_read_are_not_sent_to_ocr(
    suffix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SVG has no pixels, and Tesseract's image library does not read AVIF: no text, rather
    than a failure repeated at each indexing."""
    monkeypatch.setattr(extract, "ocr_available", lambda: True)

    def ocr(*_: object) -> str:
        raise AssertionError("sent to OCR")

    monkeypatch.setattr(extract, "ocr_image", ocr)
    assert extract_direct("image", suffix, b"\x00" * 64, "eng", True, Budget()) is None
    assert ".avif" in NO_OCR_IMAGES and ".png" not in NO_OCR_IMAGES
