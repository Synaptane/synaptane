"""Text of inbox attachments, for the tidy agent.

Without it, a PDF deposited on its own is filed blindly, from its name. Extraction happens
during the run (worker), the first time the item is read, not at deposit time: OCR of a scan
can take minutes and the deposit must answer right away. It goes through the public interface
of `services.extraction` (bounded, isolated from the process). The text is kept under
`extract_dir/<space>/.inbox/<digest>.txt`: a chained or retried run does not redo the OCR.
The directory is hidden: the wiki search (ripgrep) does not walk it.

The extracted text goes through the secret detector: a password in a PDF or a screenshot was
not seen at deposit time (only text attachments are). It is then not sent to the model, and
the item is left with the reason, for a human decision.
"""

import hashlib
import logging
from dataclasses import dataclass
from uuid import UUID

from synaptane.core.errors import Label
from synaptane.core.secret_scan import describe, scan_text
from synaptane.services import extraction
from synaptane.services.content import kind_of
from synaptane.services.workspace import Workspace

log = logging.getLogger("synaptane.tidy")

CACHE = ".inbox"
EXTRACTED = ("pdf", "office", "image")


@dataclass(frozen=True)
class AttachmentText:
    text: str | None  # None: no text (type without text, extraction failed or empty)
    extracted: bool = False  # extracted text (PDF, Office, image), not the file itself
    secrets: str = ""  # likely secrets in the extracted text (description), else empty


def attachment_text(ws: Workspace, space_id: UUID, name: str, data: bytes) -> AttachmentText:
    """Text of an attachment: the file itself if it is text, otherwise its extracted text."""
    kind = kind_of(name, data)
    if kind in ("markdown", "text"):
        # Checked at deposit time (refused, or confirmed by the person): passed on as is.
        return AttachmentText(data.decode("utf-8", "replace"))
    if kind not in EXTRACTED:
        return AttachmentText(None)
    text = _cached(ws, space_id, name, data)
    if not text or not text.strip():
        return AttachmentText(None, extracted=True)
    findings = scan_text(text, Label("attachment {name}", name=name))
    # For the model (tidy-up): the English text.
    return AttachmentText(text, extracted=True, secrets=str(describe(findings)) if findings else "")


def _cached(ws: Workspace, space_id: UUID, name: str, data: bytes) -> str:
    target = ws.extract_path(space_id) / CACHE / f"{hashlib.sha256(data).hexdigest()}.txt"
    if target.is_file():
        return target.read_text(encoding="utf-8", errors="replace")
    try:
        text = extraction.extract_text(name, data, ws) or ""
    except Exception as error:  # noqa: BLE001 — an unreadable file does not block the run
        log.warning("Extraction failed for attachment %s: %s", name, error)
        text = ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")  # even empty: do not retry on every read
    return text
