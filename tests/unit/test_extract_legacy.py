"""Old binary Word and Excel files (.doc, .xls), read by the catdoc tools in the bounded child
(.ppt files have a reader of their own: tests/unit/test_extract_ppt.py).

The fixtures (tests/fixtures/legacy/, made by scripts/legacy_files.py) are read by the real
tools when catdoc is installed. How failures are handled is tested with fake tools, written
in Python and put in the PATH, which also report the command they received."""

import json
import os
import sys
from pathlib import Path

import pytest

from synaptane import extract
from synaptane.extract import Budget, ExtractionFailed, extract_direct, extract_legacy, run_isolated
from synaptane.services.content import kind_of

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"
CATDOC = extract.legacy_available()
needs_catdoc = pytest.mark.skipif(not CATDOC, reason="catdoc absent (apt install catdoc)")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def read(name: str) -> str:
    """Text of a fixture, read like the worker reads it: in the bounded child process."""
    result = run_isolated(
        "office", Path(name).suffix, fixture(name), languages="eng", ocr_images=False
    )
    assert result.text is not None
    return result.text


def test_old_office_files_are_office_documents() -> None:
    for name in ("memo.doc", "LIST.XLS", "slides.ppt"):
        assert kind_of(name) == "office"


@needs_catdoc
def test_doc_text_is_read_whole_lines() -> None:
    text = read("printer-memo.doc")
    lines = text.splitlines()
    assert lines[0] == "Printer maintenance memo"
    # A paragraph is one line (no wrapping at 72 columns); UTF-8 accents and symbols.
    assert lines[1] == (
        "The Zanzibar colour printer on the second floor gets a new fuser unit on Monday. "
        "Print jobs go to the café printer in the meantime."
    )
    # Table cells separated by tabs.
    assert "Monday\tFuser unit replaced – 2 hours\t€ 180" in lines
    assert "Tuesday\tTest pages, Zürich office template\t€ 0" in lines


@needs_catdoc
def test_xls_rows_are_readable_lines() -> None:
    """One line per row, cells separated by tabs, without the quotes of the CSV (but with
    those in the cells), a blank line between the sheets."""
    assert read("spare-parts.xls") == (
        "Part\tQuantity\tUnit price (€)\tCabinet\tLast checked\n"
        "Toner, black\t6\t54.9\tOkavango\t2026-09-14\n"
        'Patch cable "Cat 6", 2 m\t40\t3.5\tOkavango\t2026-09-14\n'
        "Fuser unit\t1\t180\tBasement\t2026-08-03\n"
        "\n"
        "Supplier\tWebsite\n"
        "Brightrack\tbrightrack.example"
    )


@needs_catdoc
@pytest.mark.parametrize("name", ["printer-memo.doc", "spare-parts.xls"])
def test_invalid_files_fail_cleanly(name: str) -> None:
    """Not an Office file, or a damaged one: a failure with a message, never a crash, nor the
    raw bytes taken for text (catdoc copies a file that is not OLE2 as it is)."""
    suffix = Path(name).suffix
    with pytest.raises(ExtractionFailed, match="Office 97-2003"):
        run_isolated(
            "office", suffix, b"plain text, not a document", languages="eng", ocr_images=False
        )
    truncated = fixture(name)[:3000]
    with pytest.raises(ExtractionFailed):
        run_isolated("office", suffix, truncated, languages="eng", ocr_images=False)


@needs_catdoc
def test_rtf_saved_as_doc_is_read() -> None:
    """Old Word could save RTF under a .doc name: catdoc reads it (cp1252 accents)."""
    rtf = b"{\\rtf1\\ansi Quarterly memo, caf\\'e9 Zanzibar\\par}"
    text = extract_legacy(rtf, ".doc", Budget())
    assert text == "Quarterly memo, café Zanzibar"


def test_missing_catdoc_gives_no_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No text for .doc and .xls; .ppt files do not need catdoc."""
    monkeypatch.setenv("PATH", str(tmp_path))
    budget = Budget()
    for suffix in (".doc", ".xls"):
        assert extract_direct("office", suffix, b"anything", "eng", False, budget) is None
    slides = extract_direct("office", ".ppt", fixture("kickoff-slides.ppt"), "eng", False, budget)
    assert slides is not None and "Kilimanjaro" in slides
    assert not extract.legacy_available()
    assert "catdoc not installed (catdoc package): no text" in budget.notes
    result = run_isolated("office", ".xls", b"anything", languages="eng", ocr_images=False)
    assert result.text is None and result.notes == (
        "xls2csv not installed (catdoc package): no text",
    )


# --- Fake tools: how their failures are handled -----------------------------------------------

OLE = extract.OLE_SIGNATURE + bytes(504)
RECORD = """
import json, os, sys
if "FAKE_LOG" in os.environ:  # not in the child process: its environment is reduced
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
"""


def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """catdoc and xls2csv replaced by a Python script; returns its log."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    for tool in extract.LEGACY_TOOLS:
        script = folder / tool
        script.write_text(f"#!{sys.executable}\n{RECORD}\n{body}")
        script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def test_the_file_is_passed_by_a_temporary_path_after_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = fake_tools(tmp_path, monkeypatch, "print('Text of the document  ')")
    for suffix in (".doc", ".xls"):
        assert extract_legacy(OLE, suffix, Budget()) == "Text of the document"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["argv"][:-2] for call in calls] == [
        ["-w", "-s", "cp1252", "-d", "utf-8"],
        ["-d", "utf-8", "-f", "%Y-%m-%d %H:%M"],
    ]
    for call, suffix in zip(calls, (".doc", ".xls"), strict=True):
        assert call["argv"][-2] == "--"
        path = Path(call["argv"][-1])
        assert path.name == f"document{suffix}" and path.parent == Path(call["cwd"])
        assert not path.exists()  # the temporary copy is gone


def test_a_file_that_is_not_ole2_is_refused_before_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """catdoc would copy it as text: the tool is not even started (RTF aside, for .doc)."""
    log = fake_tools(tmp_path, monkeypatch, "print('copied as text')")
    budget = Budget()
    for suffix in (".doc", ".xls", ".ppt"):
        with pytest.raises(ExtractionFailed, match=f"not a \\{suffix} file from Office 97-2003"):
            extract_direct("office", suffix, b"PK\x03\x04 a .docx renamed", "eng", False, budget)
    assert not log.exists()
    assert extract_legacy(b"{\\rtf1 memo}", ".doc", budget) == "copied as text"
    with pytest.raises(ExtractionFailed):
        extract_legacy(b"{\\rtf1 memo}", ".xls", budget)


def test_password_protected_file_is_a_failure_saying_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What catdoc does (code 69) and what xls2csv does (code 0, message only)."""
    fake_tools(
        tmp_path,
        monkeypatch,
        "print('[File is encrypted. Encryption key = 00000034]', file=sys.stderr)\n"
        "sys.exit(69 if sys.argv[0].endswith('catdoc') else 0)\n",
    )
    for suffix in (".doc", ".xls"):
        with pytest.raises(ExtractionFailed, match="protected by a password"):
            extract_legacy(OLE, suffix, Budget())


def test_message_without_text_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(tmp_path, monkeypatch, "print('source is not OLE file or Error', file=sys.stderr)")
    with pytest.raises(ExtractionFailed, match="xls2csv: source is not OLE file or Error"):
        extract_legacy(OLE, ".xls", Budget())


def test_warnings_next_to_text_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(
        tmp_path,
        monkeypatch,
        "print('Format e is redefined', file=sys.stderr)\nprint('\"Asset\",\"HW-0007\"')",
    )
    assert extract_legacy(OLE, ".xls", Budget()) == "Asset\tHW-0007"


def test_silent_failure_is_reported_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tools(tmp_path, monkeypatch, "sys.exit(69)")
    with pytest.raises(ExtractionFailed, match=r"catdoc: failed \(code 69\)"):
        extract_legacy(OLE, ".doc", Budget())


def test_slow_tool_is_stopped_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tools(tmp_path, monkeypatch, "import time\ntime.sleep(30)")
    with pytest.raises(ExtractionFailed, match="catdoc: timed out"):
        extract_legacy(OLE, ".doc", Budget(timeout=1))


def test_huge_output_is_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(tmp_path, monkeypatch, "sys.stdout.write('word ' * 1000)")
    monkeypatch.setattr(extract, "MAX_LEGACY_TEXT", 100)
    budget = Budget()
    text = extract_legacy(OLE, ".doc", budget)
    assert text is not None and len(text) <= 100 and text.startswith("word word")
    assert budget.notes == ["text longer than 0 MB: truncated"]


def test_tool_inherits_the_memory_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from the child: a tool that asks for too much memory dies, the worker does not."""
    fake_tools(tmp_path, monkeypatch, "block = bytearray(3 * 1024 ** 3)\nprint('never')")
    with pytest.raises(ExtractionFailed, match="MemoryError"):
        run_isolated("office", ".xls", OLE, languages="eng", ocr_images=False, memory=512 << 20)


def test_sheet_dates_and_times_are_shortened() -> None:
    """`-f` gives every date cell a date and a time: a date alone loses its midnight, a time
    alone its day 0 (30 December 1899, or 1 January 1904 on old Macs)."""
    csv = (
        '"Day","Start","Stamp","Note"\n'
        '"2019-04-20 00:00","1899-12-30 09:30","2019-04-20 17:45","multi\nline  cell"\n'
        '"","1904-01-01 08:00",,\n'
        '"","",,\n'
        "\f"
        "\f"
    )
    assert extract._sheets(csv) == (
        "Day\tStart\tStamp\tNote\n2019-04-20\t09:30\t2019-04-20 17:45\tmulti line cell\n08:00"
    )
