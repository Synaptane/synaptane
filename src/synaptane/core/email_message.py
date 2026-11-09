"""Reading an email received by the capture (`services.email_capture`): its headers, its text
(plain text if it has one, otherwise its HTML turned into Markdown) and its attachments.

Nothing here trusts the sender: the HTML loses its scripts, styles and remote images (an image
becomes its alternative text, a tracking pixel nothing), links keep only http, https and mailto,
and the Markdown produced cannot load an image.
"""

import mimetypes
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import errors, policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

# Headers naming the recipient of this very copy (the envelope), set by the receiving server.
ENVELOPE_HEADERS = (
    "Delivered-To",
    "X-Original-To",
    "X-Delivered-To",
    "Envelope-To",
    "X-Envelope-To",
)
RECIPIENT_HEADERS = ("To", "Cc")
# Headers RFC 5322 allows once (§3.6). Twice means someone added one above the original: a
# replayed, DKIM-signed email whose added header Python would read (the first) while the
# signature covers the other (the last).
SINGLE_HEADERS = (
    "From",
    "Sender",
    "Reply-To",
    "To",
    "Cc",
    "Subject",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
)
# An inline image (in the HTML, `cid:`) smaller than this is decoration: a logo, a signature.
INLINE_MIN = 16 * 1024
MAX_NAME = 120
MAX_NAME_BYTES = 200  # a file name is limited to 255 bytes on most file systems, not characters
# Beyond these, an email is too complex to read: Python's parser takes seconds per level of
# nested parts (a few dozen kilobytes would hold a worker for minutes). Real emails nest a
# handful of parts.
MAX_BOUNDARIES = 50
MAX_PART_LINES = 2000


@dataclass
class Parsed:
    from_addresses: list[str]  # every address of every From header, lowercase
    from_headers: int  # number of From headers
    from_name: str
    subject: str
    date: datetime | None
    message_id: str
    envelope: list[str]  # addresses of the envelope headers, lowercase
    recipients: list[str]  # addresses of To and Cc, lowercase
    auth_results: list[str]  # Authentication-Results headers, topmost first
    automatic: bool  # auto-reply, bounce, mailing list: never answered, never dropped
    duplicated: tuple[str, ...] = ()  # SINGLE_HEADERS present more than once
    # A line of the headers that is not a header: Python stops reading the headers there (or
    # skips it), where another reader, a DKIM verifier say, may read on.
    broken_headers: bool = False
    received: list[str] = field(default_factory=list)  # Received headers, topmost first
    body: str = ""
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    has_content: bool = False  # body and attachments read (`parse(with_content=True)`)

    @property
    def sender(self) -> str:
        """The address of the only author; "" when there is not exactly one From header with
        exactly one valid address (two authors: whose mailbox authenticated it?)."""
        if self.from_headers != 1 or len(self.from_addresses) != 1:
            return ""
        address = self.from_addresses[0]
        local, at, domain = address.rpartition("@")
        if not at or not local or "." not in domain or any(c.isspace() for c in address):
            return ""
        return address


def _header(message: Message, name: str) -> list[str]:
    values = []
    for value in message.get_all(name) or []:
        try:
            values.append(str(value))
        except Exception:  # noqa: BLE001 — a header that cannot be decoded is left out
            continue
    return values


def _addresses(message: Message, names: tuple[str, ...]) -> list[str]:
    raw = [value for name in names for value in _header(message, name)]
    return [address.strip().lower() for _, address in getaddresses(raw) if "@" in address]


def _automatic(message: Message) -> bool:
    auto = " ".join(_header(message, "Auto-Submitted")).strip().lower()
    precedence = " ".join(_header(message, "Precedence")).strip().lower()
    return_path = " ".join(_header(message, "Return-Path")).strip()
    return (
        bool(auto and auto != "no")
        or precedence in ("bulk", "junk", "list", "auto_reply")
        or bool(_header(message, "List-Id"))
        or bool(_header(message, "X-Autoreply") or _header(message, "X-Autorespond"))
        or return_path == "<>"
        or message.get_content_type() == "multipart/report"
    )


def parse(raw: bytes, *, with_content: bool = True) -> Parsed:
    """An email, headers and (`with_content`) text and attachments. Never raises on a
    malformed email: what cannot be read is left out. Without content, the parts are not even
    parsed: cheap, whoever sent it."""
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=not with_content)
    from_headers = _header(message, "From")
    pairs = getaddresses(from_headers)
    try:
        date = parsedate_to_datetime(" ".join(_header(message, "Date")))
        date = date if date.tzinfo else date.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        date = None
    parsed = Parsed(
        from_addresses=[address.strip().lower() for _, address in pairs],
        from_headers=len(from_headers),
        from_name=" ".join((pairs[0][0] if pairs else "").split()),
        subject=" ".join(" ".join(_header(message, "Subject")).split()),
        date=date,
        message_id=" ".join(" ".join(_header(message, "Message-ID")).split())[:998],
        envelope=_addresses(message, ENVELOPE_HEADERS),
        recipients=_addresses(message, RECIPIENT_HEADERS),
        auth_results=_header(message, "Authentication-Results"),
        automatic=_automatic(message),
        duplicated=_duplicated(message),
        broken_headers=any(isinstance(d, _BROKEN) for d in message.defects),
        received=_header(message, "Received"),
    )
    if with_content and isinstance(message, EmailMessage):
        parsed.body, parsed.attachments = _content(message)
        parsed.has_content = True
    return parsed


# `To : x` (space before the colon, obsolete syntax), a line without colon, a NUL, a second
# "From " line: the headers below are not headers for Python, but may be for others; a signed
# email replayed with headers of one's own above such a line would hide its duplicates.
_BROKEN = (errors.MissingHeaderBodySeparatorDefect, errors.MisplacedEnvelopeHeaderDefect)


def _duplicated(message: Message) -> tuple[str, ...]:
    names = [name.lower() for name in message]  # every header, repeated ones included
    return tuple(h for h in SINGLE_HEADERS if names.count(h.lower()) > 1)


def received_at(received: list[str]) -> datetime | None:
    """Date of the topmost `Received` header (after its last ";"): when the receiving server
    of the mailbox took the email."""
    if not received:
        return None
    try:
        found = parsedate_to_datetime(received[0].rpartition(";")[2].strip())
    except (TypeError, ValueError, IndexError):
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def too_complex(raw: bytes) -> bool:
    """Too many parts, or parts nested too deep, to be read in a reasonable time."""
    boundaries = len(re.findall(rb"(?i)\bboundary\s*=", raw))
    return boundaries > MAX_BOUNDARIES or raw.count(b"\n--") > MAX_PART_LINES


def read_content(parsed: Parsed, raw: bytes) -> None:
    """Fills the text and attachments of an email parsed without them."""
    if parsed.has_content:
        return
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if isinstance(message, EmailMessage):
        parsed.body, parsed.attachments = _content(message)
    parsed.has_content = True


# --- Text and attachments ---------------------------------------------------------------------


def _text_of(part: Message) -> str:
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else None
    except (LookupError, ValueError, AssertionError):  # unknown charset, broken encoding
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _content(message: EmailMessage) -> tuple[str, list[tuple[str, bytes]]]:
    body_part = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body_part is not None:
        text = _text_of(body_part)
        if body_part.get_content_type() == "text/plain" and not text.strip():
            html_part = message.get_body(preferencelist=("html",))
            if html_part is not None:
                body_part, text = html_part, _text_of(html_part)
        if body_part.get_content_type() == "text/html":
            text = html_to_markdown(text)
        else:
            text = text.replace("\r\n", "\n")
    skip = {id(body_part)} | _alternatives(message, body_part)
    extra_texts: list[str] = []
    attachments: list[tuple[str, bytes]] = []
    _collect(message, skip, extra_texts, attachments)
    body = "\n\n".join(t.strip() for t in [text, *extra_texts] if t.strip())
    return defuse(body), _unique(attachments)


def _alternatives(message: EmailMessage, body: Message | None) -> set[int]:
    """The other versions of the body (the HTML of a plain text body, and the reverse)."""
    if body is None:
        return set()
    found: set[int] = set()
    for part in message.walk():
        if part.get_content_type() == "multipart/alternative" and isinstance(part, EmailMessage):
            children = list(part.iter_parts())
            if any(c is body or _contains(c, body) for c in children):
                for child in children:
                    if child.get_content_type() in ("text/plain", "text/html"):
                        found.add(id(child))
    return found


def _contains(part: Message, wanted: Message) -> bool:
    return part.is_multipart() and any(p is wanted for p in part.walk())


def _collect(
    part: EmailMessage,
    skip: set[int],
    texts: list[str],
    attachments: list[tuple[str, bytes]],
) -> None:
    kind = part.get_content_type()
    if id(part) in skip:
        return
    if kind == "message/rfc822":
        inner = part.get_payload()
        message = inner[0] if isinstance(inner, list) and inner else None
        data = message.as_bytes() if isinstance(message, Message) else b""
        subject = str(message.get("Subject", "")) if isinstance(message, Message) else ""
        name = part.get_filename() or f"{subject.strip() or 'message'}.eml"
        attachments.append((name, data))
        return
    if part.is_multipart():
        for child in part.iter_parts():
            if isinstance(child, EmailMessage):
                _collect(child, skip, texts, attachments)
        return
    disposition = part.get_content_disposition()
    name = part.get_filename() or ""
    if kind in ("text/plain", "text/html") and not name and disposition != "attachment":
        text = _text_of(part)  # Apple Mail: text, attachment, text again
        texts.append(html_to_markdown(text) if kind == "text/html" else text)
        return
    payload = part.get_payload(decode=True)
    data = payload if isinstance(payload, bytes) else b""
    if disposition != "attachment" and part.get("Content-ID") and len(data) < INLINE_MIN:
        return  # logo of a signature, decoration of the HTML
    if not name:
        extension = mimetypes.guess_extension(kind) or ".bin"
        name = f"attachment{extension}"
    attachments.append((name, data))


def clean_name(name: str) -> str:
    """A file name that can be written in the repository: no folder, no leading dot, no
    control character (nor a Unicode format character, such as a right-to-left override that
    would disguise `fdp.exe` as `exe.pdf`), at most MAX_NAME characters and MAX_NAME_BYTES
    bytes in UTF-8 (the extension kept)."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # the file name, without its folders
    name = "".join(c for c in name if unicodedata.category(c) != "Cf")
    name = re.sub(r"[\x00-\x1f\x7f:*?\"<>|]+", "_", name).strip().lstrip(".").strip()
    if not name:
        name = "attachment"
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension) > 10 or not stem:
        stem, dot, extension = name, "", ""
    stem = stem[:MAX_NAME]  # a name can be megabytes long: cut before trimming
    name = stem.rstrip() + dot + extension
    while len(name) > MAX_NAME or len(name.encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]
        name = stem.rstrip() + dot + extension
    return name


def _unique(attachments: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    seen: set[str] = set()
    found = []
    for name, data in attachments:
        name = clean_name(name)
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        candidate, number = name, 2
        while candidate.lower() in seen:
            candidate = f"{stem} ({number}){dot}{extension}"
            number += 1
        seen.add(candidate.lower())
        found.append((candidate, data))
    return found


def defuse(markdown: str) -> str:
    """Markdown that loads nothing by itself: no image (`![…](…)` becomes a plain link) and
    no raw `<img>`."""
    markdown = markdown.replace("![", "!\\[")
    return re.sub(r"<(?=\s*img\b)", "&lt;", markdown, flags=re.I)


# --- HTML to Markdown -------------------------------------------------------------------------

_SKIPPED = frozenset(
    {"script", "style", "head", "title", "noscript", "template", "svg", "iframe", "object"}
)
_BLOCKS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "form",
        "fieldset",
        "figure",
        "figcaption",
        "address",
        "center",
    }
)
_VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "wbr"})
_LINK_SCHEMES = ("http://", "https://", "mailto:")
_ESCAPED = re.compile(r"([\\`*_\[\]<])")
_URL_ESCAPES = (("(", "%28"), (")", "%29"), (" ", "%20"), ("<", "%3C"), (">", "%3E"))
# Indentation of nested lists and quotes, at most: each level repeats its prefix on every line
# inside it, and thousands of nested <ul> would weigh gigabytes.
MAX_DEPTH = 8


class _Markdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.quotes: list[int] = []  # where each open quote starts in `out`
        self.lists: list[list[int]] = []  # [kind (0: ul, 1: ol), counter]
        self.links: list[str | None] = []

    def _tail(self) -> str:
        """The last characters written (enough to know how the current line ends)."""
        tail = ""
        for piece in reversed(self.out):
            tail = piece + tail
            if len(tail) >= 4:
                break
        return tail

    def _at_line_start(self) -> bool:
        tail = self._tail()
        return not tail or tail.endswith("\n")

    def _newline(self, blank: bool = False) -> None:
        """Ends the current line (`blank`: and leaves an empty line), nothing at the start."""
        tail = self._tail()
        if not tail.strip("\n"):
            return
        have = len(tail) - len(tail.rstrip("\n"))
        need = 2 if blank else 1
        if have < need:
            self.out.append("\n" * (need - have))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self.skip += 1
            return
        if self.skip:
            return
        values = {k: v or "" for k, v in attrs}
        if tag in _BLOCKS:
            self._newline(blank=tag in ("p", "table", "ul", "ol") and not self.lists)
            if tag in ("ul", "ol"):
                self.lists.append([int(tag == "ol"), 0])
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(blank=True)
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._newline()
            depth = min(max(len(self.lists) - 1, 0), MAX_DEPTH)
            kind = self.lists[-1] if self.lists else [0, 0]
            kind[1] += 1
            self.out.append("  " * depth + (f"{kind[1]}. " if kind[0] else "- "))
        elif tag == "br":
            self.out.append("\n" if self.pre else "  \n")
        elif tag == "hr":
            self._newline(blank=True)
            self.out.append("---\n\n")
        elif tag == "blockquote":
            self._newline(blank=True)
            self.quotes.append(len(self.out))
        elif tag == "pre":
            self._newline(blank=True)
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag == "a":
            href = values.get("href", "").strip()
            safe = href if href.lower().startswith(_LINK_SCHEMES) else None
            self.links.append(safe)
            if safe:
                self.out.append("[")
        elif tag == "img":
            alt = " ".join(values.get("alt", "").split())
            if alt:
                self.out.append("[image: " + _ESCAPED.sub(r"\\\1", alt) + "]")
        elif tag in ("td", "th") and not self._at_line_start():
            self.out.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            return  # <svg/>: nothing inside
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self.skip = max(self.skip - 1, 0)
            return
        if self.skip:
            return
        if tag in _BLOCKS:
            if tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            self._newline(blank=tag in ("p", "table", "ul", "ol") and not self.lists)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(blank=True)
        elif tag == "li":
            self._newline()
        elif tag == "blockquote" and self.quotes:
            start = self.quotes.pop()
            if len(self.quotes) >= MAX_DEPTH:  # deeper: kept, without its own "> "
                return
            quoted = "".join(self.out[start:]).strip("\n")
            del self.out[start:]
            self.out.append("\n".join(f"> {line}".rstrip() for line in quoted.split("\n")))
            self._newline(blank=True)
        elif tag == "pre" and self.pre:
            self.pre -= 1
            self._newline()
            self.out.append("```\n\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href:
                for char, code in _URL_ESCAPES:
                    href = href.replace(char, code)
                self.out.append(f"]({href})")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if self._at_line_start() or self._tail().endswith(" "):
            text = text.lstrip()
        if text:
            self.out.append(_ESCAPED.sub(r"\\\1", text))


def html_to_markdown(html: str) -> str:
    """Readable Markdown from the HTML of an email: structure kept (headings, paragraphs,
    lists, quotes, links, emphasis, code), everything else dropped."""
    parser = _Markdown()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:  # HTMLParser on a broken declaration: keep what was read
        pass
    text = "".join(parser.out)
    lines = [line.rstrip() if not line.endswith("  ") else line for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
