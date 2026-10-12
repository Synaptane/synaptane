"""Reading the text of a document (PDF, Office, image) in a separate, bounded process.

PDF: pypdfium2; Office 2007+ and OpenDocument: their XML, read here; old binary Office files:
the catdoc tools (.doc, .xls), and a reader of our own for .ppt (`extract_ppt`); images and
scans: Tesseract. External programs are run from the child, so they share its limits.

Why a separate process: pdfium, Pillow and Tesseract read files dropped by any contributor.
A 432-byte PDF with a giant `MediaBox` asked for 2.4 GB at render time and got the worker
killed over and over. Each document is therefore read by a `python -m synaptane.extract`
that, before reading anything, sets itself a limit on memory (`RLIMIT_AS`, inherited by
Tesseract), CPU time and file size. The worker waits for it under an overall timeout and
kills its whole process group if it runs over. At worst, this child dies, not the worker.

Internal bounds, so that a legitimate but huge document yields text rather than a failure:
pixels capped **before** a page is rendered (`page_scale`), number of pages read, number of
pages sent to OCR and a timeout per page, and a deadline for the whole document beyond
which the native text is kept and nothing more is sent to OCR.

This module only imports the standard library at load time: the child starts fast and sees
neither the database, nor the configuration, nor the secrets (its environment is reduced).
"""

import argparse
import contextlib
import csv
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

MEMORY_LIMIT = 1024 * 1024 * 1024  # address space of the child (and Tesseract), in bytes
EXTRACT_TIMEOUT = 600  # seconds for one document, OCR included
KILL_MARGIN = 30  # past the deadline, the worker kills the child (it should have stopped)
MAX_OUTPUT_FILE = 256 * 1024 * 1024  # RLIMIT_FSIZE: rendered page, written text, catdoc output
MAX_PDF_PAGES = 500  # pages read
MAX_OCR_PAGES = 100  # pages without text sent to OCR, per document
OCR_DPI = 300
OCR_TIMEOUT = 60  # seconds per page or per image
MAX_PIXELS = 40_000_000  # image handed to Tesseract: 40 Mpx (an A4 page at 300 dpi is 8.7)
# Images not sent to OCR: SVG is text (no pixels), and AVIF is not read by Leptonica, the image
# library of Tesseract: the OCR would fail again each time the file is indexed.
NO_OCR_IMAGES = frozenset({".svg", ".avif"})
MAX_IMAGE_PIXELS = 250_000_000  # a dropped image beyond this: refused without decoding
NATIVE_TEXT = 20  # characters below which a page is taken for a scan

ERROR_TAIL = 500


class ExtractionFailed(Exception):
    """The document could not be read: timeout, memory, invalid file, child killed."""


# --- Extractors (in the child) ---------------------------------------------------------------


@dataclass
class Budget:
    """What a document has left: deadline and OCR pages. The notes go to the log."""

    timeout: float = EXTRACT_TIMEOUT
    ocr_pages: int = MAX_OCR_PAGES
    notes: list[str] = field(default_factory=list)
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.deadline = time.monotonic() + self.timeout

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image_file(path: Path, languages: str, timeout: float = OCR_TIMEOUT) -> str:
    result = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", languages, "--psm", "3"],
        capture_output=True,
        timeout=max(1.0, timeout),
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-ERROR_TAIL:])
    return result.stdout.decode("utf-8", "replace")


def page_scale(width: float, height: float) -> float:
    """Render scale of a page (in points) for OCR: 300 dpi, unless the image would exceed
    `MAX_PIXELS`. Computed before rendering: rendering is what cost 2.4 GB."""
    area = width * height
    if not area > 0 or not math.isfinite(area):
        return 0.0
    scale = min(OCR_DPI / 72, math.sqrt(MAX_PIXELS / area))
    for _ in range(1000):  # pdfium rounds up to the next pixel: tighten a little if needed
        if math.ceil(width * scale) * math.ceil(height * scale) <= MAX_PIXELS:
            break
        scale *= 0.999
    return scale


def extract_pdf(data: bytes, languages: str, budget: Budget | None = None) -> str:
    """Native text of each page; OCR of pages without text (scans), within the limits."""
    import pypdfium2 as pdfium

    budget = budget or Budget()
    ocr = ocr_available()
    document = pdfium.PdfDocument(data)
    pages: list[str] = []
    try:
        count = len(document)
        if count > MAX_PDF_PAGES:
            budget.note(f"{count} pages: only the first {MAX_PDF_PAGES} are read")
        with tempfile.TemporaryDirectory(prefix="synaptane-ocr-") as scratch:
            for index in range(min(count, MAX_PDF_PAGES)):
                page = document[index]
                try:
                    text = page.get_textpage().get_text_range().strip()
                    if len(text) < NATIVE_TEXT and ocr:
                        text = _ocr_page(page, index, Path(scratch), languages, budget) or text
                finally:
                    page.close()
                pages.append(text)
    finally:
        document.close()
    return "\n\n".join(pages)


def _ocr_page(page: object, index: int, scratch: Path, languages: str, budget: Budget) -> str:
    """OCR of a page, or "" if the budget is spent or the page does not survive it: an
    unreadable page does not lose the text of the others."""
    if budget.ocr_pages <= 0:
        budget.note(f"OCR limited to the first {MAX_OCR_PAGES} pages without text")
        return ""
    if budget.remaining() <= 1:
        budget.note(f"OCR stopped: {budget.timeout:g} s timeout reached")
        return ""
    budget.ocr_pages -= 1
    width, height = page.get_size()  # type: ignore[attr-defined]
    scale = page_scale(width, height)
    if scale <= 0:
        return ""
    target = scratch / f"page-{index}.png"
    try:
        bitmap = page.render(scale=scale, grayscale=True)  # type: ignore[attr-defined]
        bitmap.to_pil().save(target)
        del bitmap  # freed before Tesseract takes its share of the memory
        return ocr_image_file(target, languages, min(OCR_TIMEOUT, budget.remaining())).strip()
    except (RuntimeError, OSError, MemoryError, subprocess.TimeoutExpired) as error:
        budget.note(f"page {index + 1}: OCR failed ({_short(error)})")
        return ""
    finally:
        target.unlink(missing_ok=True)


def ocr_image(data: bytes, suffix: str, languages: str, budget: Budget | None = None) -> str:
    """OCR of an image. Beyond `MAX_PIXELS`, it is shrunk before Tesseract; beyond
    `MAX_IMAGE_PIXELS` (header read, nothing decoded), refused."""
    budget = budget or Budget()
    with tempfile.TemporaryDirectory(prefix="synaptane-ocr-") as scratch:
        target = Path(scratch) / f"image{suffix}"
        size = _image_size(data)
        if size and size[0] * size[1] > MAX_IMAGE_PIXELS:
            raise ExtractionFailed(f"image too large ({size[0]} × {size[1]} px)")
        if size and size[0] * size[1] > MAX_PIXELS:
            target = Path(scratch) / "image.png"
            _reduce(data, size, target)
        else:
            target.write_bytes(data)
        return ocr_image_file(target, languages, min(OCR_TIMEOUT, budget.remaining()))


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Dimensions read from the header, or None if Pillow does not know the format (Tesseract
    then handles it, still under the child's memory limit)."""
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = None  # header only: our own cap applies
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _reduce(data: bytes, size: tuple[int, int], target: Path) -> None:
    from PIL import Image

    ratio = math.sqrt(MAX_PIXELS / (size[0] * size[1]))
    wanted = (max(1, int(size[0] * ratio)), max(1, int(size[1] * ratio)))
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # our cap, not Pillow's
    with Image.open(io.BytesIO(data)) as image:
        image.draft("L", wanted)  # JPEG: shrunk while decoding
        reduced = image.convert("L")
        reduced.thumbnail(wanted)
        reduced.save(target)


_OFFICE_PARTS = {
    ".docx": ("word/document.xml",),
    ".pptx": ("ppt/slides/",),
    # Shared strings (written by Excel), and the sheets themselves: text typed in a cell can
    # also sit there inline (openpyxl, pandas and other generators), next to the numbers.
    ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/sheet"),
    ".odt": ("content.xml",),
    ".ods": ("content.xml",),
    ".odp": ("content.xml",),
}
_PARAGRAPHS = {"p", "h", "tr", "si", "row"}
MAX_PART = 20_000_000  # decompressed bytes per XML part read
MAX_TOTAL = 60_000_000  # decompressed bytes read in total, for one document


def _sheet_rows(root: ElementTree.Element) -> list[str]:
    """Text of an Excel sheet, one line per row: inline strings, formula results and numbers.
    Cells pointing to a shared string (`t="s"`) only hold its index: the text itself is read
    from xl/sharedStrings.xml."""
    rows = []
    for row in root.iter():
        if row.tag.rsplit("}", 1)[-1] != "row":
            continue
        cells = []
        for cell in row:
            if cell.tag.rsplit("}", 1)[-1] != "c" or cell.get("t") in ("s", "b", "e"):
                continue
            if cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter() if t.tag.rsplit("}", 1)[-1] == "t")
            else:  # number, or the cached result of a formula ("str")
                text = "".join(v.text or "" for v in cell if v.tag.rsplit("}", 1)[-1] == "v")
            if text.strip():
                cells.append(text.strip())
        if cells:
            rows.append("\t".join(cells))
    return rows


def extract_office(data: bytes, suffix: str, budget: Budget | None = None) -> str:
    """Plain text of Office and OpenDocument files (XML inside a zip archive).

    A booby-trapped archive ("zip bomb": a few KB that decompress into GB) does not bring the
    worker down: reading is bounded, whatever the zip headers declare."""
    lines: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(
            n for n in archive.namelist() if any(n.startswith(p) for p in _OFFICE_PARTS[suffix])
        )
        for name in names:
            if not name.endswith(".xml"):
                continue
            with archive.open(name) as part:
                raw = part.read(MAX_PART + 1)
            total += len(raw)
            if len(raw) > MAX_PART or total > MAX_TOTAL:
                if budget is not None:
                    budget.note("document too large once decompressed: skipped")
                break
            root = ElementTree.fromstring(raw)
            if name.startswith("xl/worksheets/"):
                lines.extend(_sheet_rows(root))
                continue
            current: list[str] = []
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in _PARAGRAPHS and current:
                    lines.append("".join(current).strip())
                    current = []
                if element.text and tag in ("t", "span", "p", "h", "v"):
                    current.append(element.text)
                if element.tail and tag == "span":
                    current.append(element.tail)
            if current:
                lines.append("".join(current).strip())
    return "\n".join(line for line in lines if line)


# Old binary Office formats (97-2003, and Word or Excel 95 before them). Word and Excel: read
# by the catdoc tools, run from this child, so under its limits (memory, CPU time, size of the
# files written). The file is passed by the path of its temporary copy, never by a name a
# person chose; after `--`, so it is never taken for an option. Source charset of 8-bit text
# (Word 6/95, RTF): cp1252, as on Debian, rather than the cp1251 compiled in by default.
# PowerPoint: read in Python by `extract_ppt` (catppt only sees the outline: neither text
# boxes, nor tables, nor notes, nor anything in a file saved by LibreOffice).
LEGACY_OFFICE = {
    ".doc": ("catdoc", "-w", "-s", "cp1252", "-d", "utf-8"),
    ".xls": ("xls2csv", "-d", "utf-8", "-f", "%Y-%m-%d %H:%M"),
}
LEGACY_TOOLS = ("catdoc", "xls2csv")
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MAX_LEGACY_TEXT = 20_000_000  # bytes of text kept from the output of a catdoc tool


def legacy_available() -> bool:
    return all(shutil.which(tool) is not None for tool in LEGACY_TOOLS)


def _check_ole(data: bytes, suffix: str) -> None:
    """A file that is not an OLE2 document is refused before it is read (catdoc would copy it
    as text); RTF saved under a .doc name aside."""
    if not data.startswith(OLE_SIGNATURE) and not (suffix == ".doc" and data.startswith(b"{\\rtf")):
        raise ExtractionFailed(f"not a {suffix} file from Office 97-2003 (OLE2 document)")


def extract_powerpoint(data: bytes, budget: Budget | None = None) -> str:
    """Text of a PowerPoint 97-2003 file: slides, text boxes, tables, speaker notes. A damaged
    file gives no text (noted); a password-protected one is a failure saying so."""
    from synaptane import extract_ppt as reader

    _check_ole(data, ".ppt")
    try:
        return reader.ppt_text(data, budget)
    except reader.Encrypted:
        raise ExtractionFailed("protected by a password, no text") from None


def extract_legacy(data: bytes, suffix: str, budget: Budget | None = None) -> str | None:
    """Text of an old Word or Excel file (.doc, .xls), or None if catdoc is not installed.

    The tools say little when they fail (some exit with code 0 after an error message): a
    file they cannot read (password, damaged) raises `ExtractionFailed` with their message.
    A file that is not an OLE2 document is refused before: catdoc would copy it as text."""
    budget = budget or Budget()
    command = LEGACY_OFFICE[suffix]
    if shutil.which(command[0]) is None:
        budget.note(f"{command[0]} not installed (catdoc package): no text")
        return None
    _check_ole(data, suffix)
    with tempfile.TemporaryDirectory(prefix="synaptane-catdoc-") as scratch:
        source = Path(scratch) / f"document{suffix}"
        source.write_bytes(data)
        output, errors = Path(scratch) / "text", Path(scratch) / "errors"
        try:
            with output.open("wb") as stdout, errors.open("wb") as stderr:
                code = subprocess.run(
                    [*command, "--", str(source)],
                    cwd=scratch,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=max(1.0, budget.remaining()),
                ).returncode
        except subprocess.TimeoutExpired:
            raise ExtractionFailed(f"{command[0]}: timed out") from None
        with output.open("rb") as stream:
            raw = stream.read(MAX_LEGACY_TEXT + 1)
        with errors.open("rb") as stream:
            message = _tool_message(stream.read(64 * 1024))
    if len(raw) > MAX_LEGACY_TEXT:
        raw = raw[:MAX_LEGACY_TEXT]
        budget.note(f"text longer than {MAX_LEGACY_TEXT // 1_000_000} MB: truncated")
    if code != 0 or (not raw.strip() and message):
        if "encrypted" in message.lower():
            raise ExtractionFailed(f"{command[0]}: protected by a password, no text")
        raise ExtractionFailed(f"{command[0]}: {message or f'failed (code {code})'}")
    text = raw.decode("utf-8", "replace")
    if suffix == ".xls":
        text = _sheets(text)
    return _tidy(text)


def _tool_message(stderr: bytes) -> str:
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    return lines[-1].strip()[:ERROR_TAIL] if lines else ""


_DATE_ONLY = re.compile(r"^(\d{4}-\d{2}-\d{2}) 00:00$")
_TIME_ONLY = re.compile(r"^(?:1899-12-3[01]|1904-01-01) (\d{2}:\d{2})$")  # day 0 of Excel


def _sheets(text: str) -> str:
    """xls2csv's CSV (a form feed after each sheet), made readable like the .xlsx: one line per
    row, its non-empty cells separated by tabs, a blank line between sheets. Dates come out as
    `-f` asks (date and time), then lose a midnight time or the day 0 of a time alone."""
    sheets = []
    for sheet in text.split("\f"):
        rows = []
        for row in csv.reader(io.StringIO(sheet)):
            cells = []
            for cell in row:
                value = " ".join(cell.split())
                value = _DATE_ONLY.sub(r"\1", _TIME_ONLY.sub(r"\1", value))
                if value:
                    cells.append(value)
            if cells:
                rows.append("\t".join(cells))
        if rows:
            sheets.append("\n".join(rows))
    return "\n\n".join(sheets)


def _tidy(text: str) -> str:
    """Page or slide breaks (form feeds) become blank lines; no trailing spaces, no more than
    one blank line in a row."""
    lines = [line.rstrip() for line in text.replace("\f", "\n\n").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_direct(
    kind: str, suffix: str, data: bytes, languages: str, ocr_images: bool, budget: Budget
) -> str | None:
    """Text of a document, in the current process: this is what the child does."""
    if kind == "pdf":
        return extract_pdf(data, languages, budget)
    if kind == "office" and suffix in _OFFICE_PARTS:
        return extract_office(data, suffix, budget)
    if kind == "office" and suffix in LEGACY_OFFICE:
        return extract_legacy(data, suffix, budget)
    if kind == "office" and suffix == ".ppt":
        return extract_powerpoint(data, budget)
    if kind == "image" and suffix not in NO_OCR_IMAGES and ocr_images and ocr_available():
        return ocr_image(data, suffix, languages, budget)
    return None


# --- Worker side: starting the child --------------------------------------------------------


@dataclass(frozen=True)
class Result:
    text: str | None  # None: this file type has no text (or OCR is disabled)
    notes: tuple[str, ...] = ()


def _child_env() -> dict[str, str]:
    """Reduced environment: no secrets, no database URL, no worker HOME. Only what is needed
    to find Python, this package, Tesseract and the catdoc tools."""
    package_root = str(Path(__file__).resolve().parents[1])
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": package_root,  # the same code as the worker, even when run from the sources
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_THREAD_LIMIT": "1",  # Tesseract: one thread, predictable memory
    }
    if "TESSDATA_PREFIX" in os.environ:
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
    return env


def run_isolated(
    kind: str,
    suffix: str,
    data: bytes,
    *,
    languages: str,
    ocr_images: bool,
    timeout: int = EXTRACT_TIMEOUT,
    memory: int = MEMORY_LIMIT,
) -> Result:
    """Read a document in a bounded child process. Raise `ExtractionFailed` on failure,
    never leaving the child (or an orphaned Tesseract) running behind."""
    with tempfile.TemporaryDirectory(prefix="synaptane-extract-") as scratch:
        source = Path(scratch) / f"source{suffix if suffix.isascii() else ''}"
        output = Path(scratch) / "result.json"
        source.write_bytes(data)
        command = [
            sys.executable,
            "-P",  # not the current folder in sys.path
            "-m",
            "synaptane.extract",
            f"--kind={kind}",
            f"--suffix={suffix}",
            f"--languages={languages}",
            f"--timeout={timeout}",
            f"--memory={memory}",
            *(["--ocr-images"] if ocr_images else []),
            str(source),
            str(output),
        ]
        process = subprocess.Popen(
            command,
            cwd=scratch,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # a group of its own: Tesseract is killed too
        )
        try:
            _, stderr = process.communicate(timeout=timeout + KILL_MARGIN)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.communicate()
            raise ExtractionFailed(f"timed out ({timeout} s)") from None
        except BaseException:
            _kill_group(process)
            process.communicate()
            raise
        if process.returncode != 0:
            raise ExtractionFailed(_failure(process.returncode, stderr, memory))
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ExtractionFailed(f"unreadable result ({_short(error)})") from None
    text = payload.get("text")
    return Result(
        text if isinstance(text, str) else None, tuple(str(n) for n in payload.get("notes", []))
    )


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _failure(code: int, stderr: bytes, memory: int) -> str:
    message = stderr.decode("utf-8", "replace").strip().splitlines()
    detail = message[-1][-ERROR_TAIL:] if message else ""
    if code == MEMORY_EXIT:
        return f"out of memory ({memory // (1024 * 1024)} MB limit)"
    if code < 0:
        name = signal.Signals(-code).name if -code in signal.valid_signals() else str(-code)
        return f"reading stopped by signal {name}" + (f" ({detail})" if detail else "")
    return detail or f"reading failed (code {code})"


def _short(error: BaseException) -> str:
    text = str(error).strip().splitlines()
    return (text[-1] if text else type(error).__name__)[:200]


# --- Child side ------------------------------------------------------------------------------

MEMORY_EXIT = 3
FAILURE_EXIT = 2


def _limit(memory: int, timeout: int) -> None:
    """Limits the child sets on itself, before reading the document. Inherited by Tesseract.
    No `preexec_fn` on the worker side: it is not safe in a multi-threaded process."""
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (timeout + KILL_MARGIN, timeout + 2 * KILL_MARGIN))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE, MAX_OUTPUT_FILE))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synaptane.extract")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--suffix", default="")
    parser.add_argument("--languages", default="fra+eng")
    parser.add_argument("--ocr-images", action="store_true")
    parser.add_argument("--timeout", type=int, default=EXTRACT_TIMEOUT)
    parser.add_argument("--memory", type=int, default=MEMORY_LIMIT)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    _limit(args.memory, args.timeout)
    budget = Budget(args.timeout)
    try:
        text = extract_direct(
            args.kind,
            args.suffix,
            args.source.read_bytes(),
            args.languages,
            args.ocr_images,
            budget,
        )
    except MemoryError:
        return MEMORY_EXIT
    except ExtractionFailed as error:
        print(error, file=sys.stderr)
        return FAILURE_EXIT
    except Exception as error:  # noqa: BLE001 — any failure becomes a message for the worker
        print(f"{type(error).__name__}: {_short(error)}", file=sys.stderr)
        return FAILURE_EXIT
    args.output.write_text(
        json.dumps({"text": text, "notes": budget.notes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
