"""Was an email really sent by the owner of its `From` address? Read from the
`Authentication-Results` header (RFC 8601) that the receiving server of the capture mailbox
adds, never from what the sender wrote.

A sender can write any header, an `Authentication-Results` saying `dmarc=pass` included. So:
- only the **topmost** `Authentication-Results` header is read: the receiving server adds its
  own above every header the message arrived with (and RFC 8601 asks it to remove those that
  claim its name);
- it must carry an identifier (`authserv-id`) the super admin trusts (`mx.google.com`, or
  `*.messagingengine.com` for any server of a domain).
  Microsoft 365 writes it without any identifier: such a header is only read when the super
  admin says so (`unnamed`), and only if it is the topmost one;
- the message is authenticated by `dmarc=pass` for the `From` domain (named by `header.from`),
  or by `dkim=pass` with a signing domain aligned on it; `spf=pass` alone only counts for the
  internal domains the super admin listed (an SPF pass is about the envelope, not about
  `From`);
- a header that could be read two ways (a quote inside a comment, a comment left open) or
  that holds two DMARC results authenticates nothing.
"""

import re
from dataclasses import dataclass, field

DMARC_PASS = "dmarc"
DKIM_PASS = "dkim"
SPF_PASS = "spf"


@dataclass(frozen=True)
class Result:
    method: str  # dmarc, dkim, spf… (lowercase, without version)
    result: str  # pass, fail, none… (lowercase)
    props: dict[str, str] = field(default_factory=dict)  # header.from, header.d, smtp.mailfrom…


@dataclass(frozen=True)
class Header:
    authserv_id: str | None  # None: the header has none (Microsoft 365)
    results: tuple[Result, ...]


class Ambiguous(ValueError):
    """A header that could be read two ways: a double quote inside a comment, or a comment or
    a quoted string left open."""


def _strip_comments(value: str) -> str:
    """Removes the (comments), nested or not, outside quoted strings. Raises Ambiguous when a
    comment holds a double quote, or when a comment or a quoted string is left open.

    RFC 5322 does not read quoted strings inside a comment: `(domain of "a) ; dmarc=pass (b"@x)`
    closes the comment at the first ")". A receiving server that copies a quoted address
    (`smtp.mailfrom`, HELO…) into a comment without escaping its parentheses would thus let
    the sender write a result of their own: such a header is not read at all."""
    out: list[str] = []
    depth, quoted, escaped = 0, False, False
    for char in value:
        if escaped:
            escaped = False
            if depth == 0:
                out.append(char)
            continue
        if char == "\\":
            escaped = True
            if depth == 0:
                out.append(char)
            continue
        if quoted:
            if char == '"':
                quoted = False
            out.append(char)
            continue
        if char == '"':
            if depth:
                raise Ambiguous(value)
            quoted = True
            out.append(char)
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            out.append(" ")
        elif depth == 0:
            out.append(char)
    if depth or quoted:
        raise Ambiguous(value)
    return "".join(out)


def _split(value: str, separator: str) -> list[str]:
    """Splits on `separator` outside quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


_TOKEN = re.compile(r'([A-Za-z0-9._/-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s;]+)')


def _result(text: str) -> Result | None:
    pairs = _TOKEN.findall(text)
    if not pairs:
        return None
    method, result = pairs[0]
    method = method.split("/", 1)[0].lower()  # "dkim/1" → "dkim"
    props: dict[str, str] = {}
    for key, raw in pairs[1:]:
        value = raw[1:-1].replace('\\"', '"') if raw.startswith('"') else raw
        props.setdefault(key.lower(), value)
    return Result(method, result.strip('"').lower(), props)


def parse(value: str) -> Header:
    """One `Authentication-Results` header. Lenient: what cannot be read is left out; an
    ambiguous header (see `_strip_comments`) keeps its identifier but no result."""
    try:
        text = " ".join(_strip_comments(value).split())
    except Ambiguous:
        words = value.strip().split(";", 1)[0].split()
        named = bool(words) and "=" not in words[0] and "(" not in words[0]
        return Header(words[0].lower() if named else None, ())
    segments = [s.strip() for s in _split(text, ";")]
    first = segments[0] if segments else ""
    if "=" in first.split(" ", 1)[0]:  # no identifier: the first segment is a result
        authserv_id, rest = None, segments
    else:
        authserv_id, rest = (first.split(" ", 1)[0].lower() or None), segments[1:]
    results = tuple(r for r in (_result(s) for s in rest if s and s.lower() != "none") if r)
    return Header(authserv_id, results)


def _domain(value: str) -> str:
    """Domain of `user@domain`, `@domain` or `domain`, lowercase, without the final dot."""
    return value.rsplit("@", 1)[-1].strip().strip("<>").rstrip(".").lower()


def aligned(signer: str, author: str) -> bool:
    """Relaxed alignment (RFC 7489): one domain is the other or one of its parents. A bare
    top-level domain ("com") never aligns."""
    signer, author = _domain(signer), _domain(author)
    if not signer or not author or "." not in signer or "." not in author:
        return False
    return signer == author or author.endswith("." + signer) or signer.endswith("." + author)


def in_domains(domain: str, domains: tuple[str, ...]) -> bool:
    domain = _domain(domain)
    return any(domain == d or domain.endswith("." + d) for d in domains)


def trusted_header(
    headers: list[str], trusted_ids: tuple[str, ...], unnamed: bool = False
) -> Header | None:
    """The topmost `Authentication-Results` header, if it was written by a trusted server;
    None otherwise (absent, or written by someone else)."""
    if not headers:
        return None
    top = parse(headers[0])
    if top.authserv_id is None:
        return top if unnamed else None
    return top if trusted(top.authserv_id, trusted_ids) else None


def trusted(authserv_id: str, trusted_ids: tuple[str, ...]) -> bool:
    """`authserv_id` is one of `trusted_ids`; `*.example.com` stands for any server of that
    domain (Fastmail writes mx1…mx6.messagingengine.com)."""
    authserv_id = authserv_id.lower()
    for wanted in trusted_ids:
        wanted = wanted.lower()
        if wanted.startswith("*."):
            if authserv_id.endswith(wanted[1:]) and len(authserv_id) > len(wanted) - 1:
                return True
        elif authserv_id == wanted:
            return True
    return False


def verdict(header: Header | None, from_domain: str, spf_domains: tuple[str, ...] = ()) -> str:
    """How the `From` domain is authenticated: DMARC_PASS, DKIM_PASS, SPF_PASS, or "" (not
    authenticated). An explicit `dmarc=fail` wins over everything.

    A server evaluates DMARC once per email: two `dmarc` results mean that one was written by
    someone else, and nothing is trusted. A `dmarc=pass` counts only if it names the `From`
    domain it was evaluated for (`header.from`, written by every common server): the server may
    have read a malformed `From` (`alice@acme.test <eve@evil.test>`) otherwise than here."""
    if header is None or not from_domain:
        return ""
    from_domain = _domain(from_domain)
    dmarc = [r for r in header.results if r.method == "dmarc"]
    if len(dmarc) > 1 or any(r.result in ("fail", "quarantine", "reject") for r in dmarc):
        return ""
    for result in dmarc:
        claimed = result.props.get("header.from", "")
        if result.result == "pass" and claimed and _domain(claimed) == from_domain:
            return DMARC_PASS
    for result in header.results:
        if result.method != "dkim" or result.result != "pass":
            continue
        signer = result.props.get("header.d") or result.props.get("header.i", "")
        if signer and aligned(signer, from_domain):
            return DKIM_PASS
    if spf_domains and in_domains(from_domain, spf_domains):
        for result in header.results:
            if result.method != "spf" or result.result != "pass":
                continue
            envelope = result.props.get("smtp.mailfrom") or result.props.get("smtp.helo", "")
            if envelope and aligned(envelope, from_domain):
                return SPF_PASS
    return ""


def identifiers(value: str) -> tuple[str, ...]:
    """A list typed by the super admin (spaces, commas or new lines), lowercase, without
    duplicates."""
    found: list[str] = []
    for part in re.split(r"[\s,;]+", value.lower()):
        part = part.strip().strip(".")
        if part and part not in found:
            found.append(part)
    return tuple(found)
