"""Capture by email, without a database: who sent an email (`core.authres`), what it says
(`core.email_message`) and which space it aims at (`services.email_capture.targets`)."""

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from synaptane.core import authres
from synaptane.core.email_message import (
    SINGLE_HEADERS,
    Parsed,
    clean_name,
    defuse,
    html_to_markdown,
    parse,
    read_content,
    received_at,
    too_complex,
)
from synaptane.services.email_capture import addressed, find_space, targets
from tests.fakeimap import CAPTURE, TRUSTED, message

GMAIL = (
    "mx.google.com; dkim=pass header.i=@acme.test header.s=s1 header.b=AbC; "
    "spf=pass (google.com: domain of alice@acme.test designates 1.2.3.4 as permitted sender) "
    "smtp.mailfrom=alice@acme.test; dmarc=pass (p=REJECT sp=REJECT dis=NONE) "
    "header.from=acme.test"
)
M365 = (
    "spf=pass (sender IP is 40.107.1.2) smtp.mailfrom=acme.test; dkim=pass (signature was "
    "verified) header.d=acme.test;dmarc=pass action=none header.from=acme.test;compauth=pass "
    "reason=100"
)


# --- Authentication-Results -------------------------------------------------------------------


def test_parse_reads_identifier_methods_and_properties_without_comments() -> None:
    header = authres.parse(GMAIL)
    assert header.authserv_id == "mx.google.com"
    methods = {r.method: r for r in header.results}
    assert methods["dkim"].result == "pass" and methods["dkim"].props["header.i"] == "@acme.test"
    assert methods["spf"].props["smtp.mailfrom"] == "alice@acme.test"  # comment dropped
    assert methods["dmarc"].props["header.from"] == "acme.test"


def test_parse_microsoft_365_header_has_no_identifier() -> None:
    header = authres.parse(M365)
    assert header.authserv_id is None
    assert [r.method for r in header.results] == ["spf", "dkim", "dmarc", "compauth"]


@pytest.mark.parametrize(
    "value",
    [
        "mx.example.com 1; none",
        "mx.example.com; dkim=pass (a (nested) comment; with a semicolon) header.d=x.test",
        'mx.example.com; dkim=pass reason="a \\"quoted\\" ; reason" header.d=x.test',
        "",
        ";;;",
        "mx.example.com; =pass; dkim",
    ],
)
def test_parse_is_lenient(value: str) -> None:
    header = authres.parse(value)
    for result in header.results:
        assert result.method and result.result
    if "header.d=x.test" in value:
        assert header.results[0].props["header.d"] == "x.test"


def test_only_the_topmost_header_of_a_trusted_server_counts() -> None:
    trusted = ("mx.google.com",)
    forged = "mx.google.com; dmarc=pass header.from=acme.test"
    real_fail = "mx.google.com; dmarc=fail header.from=acme.test"
    # The sender wrote its own header: the receiving server's sits above it.
    found = authres.trusted_header([real_fail, forged], trusted)
    assert found is not None and authres.verdict(found, "acme.test") == ""
    # A header of another server on top: nothing is trusted, even a trusted one below.
    assert authres.trusted_header(["evil.test; dmarc=pass", forged], trusted) is None
    assert authres.trusted_header([], trusted) is None
    # Identifier compared without case.
    assert authres.trusted_header(["MX.Google.COM; dmarc=pass"], trusted) is not None
    # Without identifier (Microsoft 365): only if asked.
    assert authres.trusted_header([M365], trusted) is None
    assert authres.trusted_header([M365], trusted, unnamed=True) is not None
    assert authres.trusted_header([forged, M365], (), unnamed=True) is None
    # Any server of a domain: `*.domain`, never the domain itself nor a lookalike.
    fastmail = ("*.messagingengine.com",)
    assert authres.trusted_header(["mx6.messagingengine.com; dmarc=pass"], fastmail)
    for other in ("messagingengine.com", "mx6.evilmessagingengine.com", "messagingengine.com.x"):
        assert authres.trusted_header([f"{other}; dmarc=pass"], fastmail) is None


@pytest.mark.parametrize(
    ("results", "domain", "expected"),
    [
        ("dmarc=pass header.from=acme.test", "acme.test", "dmarc"),
        # header.from not given: which From did the server read? (see the next test)
        ("dmarc=pass", "acme.test", ""),
        # A server evaluates DMARC once: a second result was written by someone else.
        ("dmarc=pass header.from=acme.test; dmarc=none header.from=acme.test", "acme.test", ""),
        ("dmarc=pass header.from=evil.test", "acme.test", ""),  # about another domain
        ("dmarc=fail header.from=acme.test; dkim=pass header.d=acme.test", "acme.test", ""),
        ("dmarc=bestguesspass header.from=acme.test", "acme.test", ""),
        ("dkim=pass header.d=acme.test", "acme.test", "dkim"),
        ("dkim=pass header.d=mail.acme.test", "acme.test", "dkim"),  # relaxed alignment
        ("dkim=pass header.d=acme.test", "eu.acme.test", "dkim"),
        ("dkim=pass header.i=@acme.test", "acme.test", "dkim"),
        ("dkim=pass header.d=evil.test", "acme.test", ""),  # signed, but by someone else
        ("dkim=pass header.d=notacme.test", "acme.test", ""),
        ("dkim=pass header.d=test", "acme.test", ""),  # a bare top-level domain
        ("dkim=fail header.d=acme.test", "acme.test", ""),
        ("dkim=pass", "acme.test", ""),
        ("spf=pass smtp.mailfrom=alice@acme.test", "acme.test", ""),  # SPF alone: no
        ("none", "acme.test", ""),
    ],
)
def test_verdict(results: str, domain: str, expected: str) -> None:
    header = authres.parse(f"mx.test; {results}")
    assert authres.verdict(header, domain) == expected


def test_a_from_read_differently_by_the_server_is_not_authenticated() -> None:
    """`alice@acme.test <eve@evil.test>` is Alice for Python, Eve for most servers: the DMARC
    pass of evil.test must not count for acme.test, nor a DMARC pass that names no domain."""
    parsed = parse(message(sender="alice@acme.test <eve@evil.test>"))
    assert parsed.sender == "alice@acme.test"
    for results in (
        "dmarc=pass header.from=evil.test",
        "dmarc=pass",
        "dkim=pass header.d=evil.test",
    ):
        header = authres.trusted_header([f"mx.test; {results}"], ("mx.test",))
        assert authres.verdict(header, "acme.test") == ""


@pytest.mark.parametrize(
    "value",
    [
        # A quoted address copied into a comment, its parenthesis unescaped: RFC 5322 closes
        # the comment there, and the sender's own result would follow.
        'mx.test; spf=pass (domain of "a) ; dmarc=pass header.from=acme.test (b"@evil.test '
        "designates 1.2.3.4) smtp.mailfrom=b@evil.test",
        "mx.test; dkim=pass (comment left open header.d=acme.test",
        'mx.test; dkim=pass reason="quote left open header.d=acme.test',
    ],
)
def test_an_ambiguous_header_authenticates_nothing(value: str) -> None:
    header = authres.parse(value)
    assert header.authserv_id == "mx.test" and header.results == ()
    assert authres.verdict(header, "acme.test") == ""


def test_spf_alone_only_for_the_internal_domains_listed() -> None:
    header = authres.parse("mx.test; spf=pass smtp.mailfrom=alice@corp.test; dkim=none")
    assert authres.verdict(header, "corp.test") == ""
    assert authres.verdict(header, "corp.test", ("corp.test",)) == "spf"
    assert authres.verdict(header, "eu.corp.test", ("corp.test",)) == "spf"
    assert authres.verdict(header, "other.test", ("corp.test",)) == ""
    unaligned = authres.parse("mx.test; spf=pass smtp.mailfrom=bounce@mailer.test")
    assert authres.verdict(unaligned, "corp.test", ("corp.test",)) == ""
    failed = authres.parse("mx.test; spf=softfail smtp.mailfrom=alice@corp.test")
    assert authres.verdict(failed, "corp.test", ("corp.test",)) == ""
    assert authres.verdict(None, "corp.test", ("corp.test",)) == ""


def test_identifiers_lists() -> None:
    assert authres.identifiers(" MX.Google.com, mx.google.com\nmx2.test; ") == (
        "mx.google.com",
        "mx2.test",
    )
    assert authres.identifiers("") == ()


# --- Reading an email -------------------------------------------------------------------------


def test_parse_headers_and_plain_text() -> None:
    raw = message(
        subject="=?utf-8?q?R=C3=A9union_budget?=",
        headers={"Delivered-To": "brain+acme.infra@capture.test"},
    )
    parsed = parse(raw)
    assert parsed.sender == "alice@acme.test"
    assert parsed.from_name == "Alice"
    assert parsed.subject == "Réunion budget"
    assert parsed.date is not None and parsed.date.year == 2026
    assert parsed.message_id.endswith("@acme.test>")
    assert parsed.envelope == ["brain+acme.infra@capture.test"]
    assert parsed.recipients == ["brain+acme.infra@capture.test"]
    assert parsed.auth_results[0].startswith(TRUSTED)
    assert not parsed.automatic
    assert parsed.body == "Le NAS passe à 8 To."


@pytest.mark.parametrize(
    "senders",
    [
        ["Alice <alice@acme.test>, Eve <eve@evil.test>"],  # two authors in one header
        ["alice@acme.test", "eve@evil.test"],  # two From headers
        [],  # none
        ["undisclosed-recipients:;"],
        ["Alice <alice>"],
    ],
)
def test_no_single_sender(senders: list[str]) -> None:
    mail = EmailMessage()
    if senders:
        mail["From"] = senders[0]
    mail["Subject"] = "x"
    mail.set_content("x")
    raw = mail.as_bytes()
    if len(senders) > 1:  # EmailMessage refuses a second From: add it by hand
        raw = b"From: " + senders[1].encode() + b"\r\n" + raw
    assert parse(raw).sender == ""


def test_display_name_cannot_pass_for_the_address() -> None:
    parsed = parse(message(sender='"alice@acme.test" <eve@evil.test>'))
    assert parsed.sender == "eve@evil.test"


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"List-Id": "<team.lists.acme.test>"},
        {"Return-Path": "<>"},
        {"X-Autoreply": "yes"},
    ],
)
def test_automatic_messages(headers: dict[str, str]) -> None:
    assert parse(message(headers=headers)).automatic
    assert not parse(message(headers={"Auto-Submitted": "no"})).automatic


def test_html_only_becomes_markdown_and_plain_text_is_preferred() -> None:
    html = "<p>Bonjour <b>Alice</b></p><script>alert(1)</script>"
    both = parse(message(body="Version texte", html=html))
    assert both.body == "Version texte"  # text/plain when there is one
    mail = EmailMessage()
    mail["From"] = "alice@acme.test"
    mail.set_content(html, subtype="html")
    only = parse(mail.as_bytes())
    assert only.body == "Bonjour **Alice**"
    empty_text = parse(message(body="  \n", html=html))
    assert empty_text.body == "Bonjour **Alice**"  # empty text part: the HTML instead


def test_encodings_and_charsets() -> None:
    mail = EmailMessage()
    mail["From"] = "alice@acme.test"
    mail.set_content("Café à 8 €", charset="iso-8859-15", cte="quoted-printable")
    assert parse(mail.as_bytes()).body == "Café à 8 €"
    broken = (
        b"From: alice@acme.test\r\nContent-Type: text/plain; charset=unknown-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\nCaf\xc3\xa9\r\n"
    )
    assert "Caf" in parse(broken).body  # unknown charset: read anyway
    base64 = EmailMessage()
    base64["From"] = "alice@acme.test"
    base64.set_content("Texte en base64 – ok", cte="base64")
    assert parse(base64.as_bytes()).body == "Texte en base64 – ok"


def test_attachments_inline_images_and_forwarded_emails() -> None:
    raw = message(
        html='<p>Voir <img src="cid:logo" alt="Logo"> et la capture</p>',
        attachments=[
            ("devis.pdf", "application/pdf", b"%PDF-1.4 devis"),
            ("devis.pdf", "application/pdf", b"%PDF-1.4 autre"),  # same name: renamed
            (".htaccess", "text/plain", b"deny"),  # hidden name: made visible
            ("../../etc/passwd", "application/octet-stream", b"x"),
        ],
    )
    mail = parse(raw)
    names = [name for name, _ in mail.attachments]
    assert names == ["devis.pdf", "devis (2).pdf", "htaccess", "passwd"]
    assert all("/" not in n and not n.startswith(".") for n in names)
    assert dict(mail.attachments)["devis.pdf"] == b"%PDF-1.4 devis"

    related = EmailMessage()
    related["From"] = "alice@acme.test"
    related.set_content("texte")
    related.add_alternative('<p>Bonjour <img src="cid:a"></p>', subtype="html")
    html_part = related.get_payload()[1]  # type: ignore[index]
    assert isinstance(html_part, EmailMessage)
    for data, cid, name in ((b"x" * 100, "<a>", "logo.png"), (b"y" * 20_000, "<b>", "capture.png")):
        html_part.add_related(data, "image", "png", cid=cid, filename=name, disposition="inline")
    kept = parse(related.as_bytes())
    assert [name for name, _ in kept.attachments] == ["capture.png"]  # the logo is decoration

    inner = EmailMessage()
    inner["From"] = "bob@globex.test"
    inner["Subject"] = "Contrat"
    inner.set_content("Le contrat")
    outer = EmailMessage()
    outer["From"] = "alice@acme.test"
    outer.set_content("Pour info")
    outer.add_attachment(inner)
    forwarded = parse(outer.as_bytes())
    assert forwarded.body == "Pour info"  # the forwarded email's text stays in its file
    [(name, data)] = forwarded.attachments
    assert name == "Contrat.eml" and b"Le contrat" in data


def test_extra_text_parts_are_kept_and_unnamed_attachments_named() -> None:
    raw = (
        b"From: alice@acme.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nAvant\r\n"
        b"--b\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\niVBORw==\r\n"
        b"--b\r\nContent-Type: text/plain\r\n\r\nApres\r\n--b--\r\n"
    )
    parsed = parse(raw)
    assert parsed.body == "Avant\n\nApres"
    assert [name for name, _ in parsed.attachments] == ["attachment.png"]


def test_headers_only() -> None:
    raw = message(attachments=[("a.pdf", "application/pdf", b"x")])
    parsed = parse(raw, with_content=False)
    assert parsed.sender == "alice@acme.test" and parsed.body == "" and parsed.attachments == []
    assert not parse(raw, with_content=False).has_content
    read_content(parsed, raw)
    assert parsed.has_content and parsed.body == "Le NAS passe à 8 To."
    assert [name for name, _ in parsed.attachments] == ["a.pdf"]


def _nested(levels: int) -> bytes:
    head = b"From: alice@acme.test\r\nMIME-Version: 1.0\r\n"
    out = head + b'Content-Type: multipart/mixed; boundary="b0"\r\n\r\n'
    for i in range(1, levels):
        out += f'--b{i - 1}\r\nContent-Type: multipart/mixed; boundary="b{i}"\r\n\r\n'.encode()
    out += f"--b{levels - 1}\r\nContent-Type: text/plain\r\n\r\nhi\r\n".encode()
    return out + b"".join(f"--b{i}--\r\n".encode() for i in reversed(range(levels)))


def test_too_complex_emails_are_not_parsed() -> None:
    """A thousand nested parts weigh 67 KB and hold Python's parser for half a minute: the
    headers are read without the parts, and the parts are counted before being read."""
    bomb = _nested(1000)
    assert parse(bomb, with_content=False).sender == "alice@acme.test"  # instant
    assert too_complex(bomb)
    many = b"Content-Type: multipart/mixed; boundary=b\r\n\r\n" + b"--b\r\n\r\nx\r\n" * 3000
    assert too_complex(many)
    assert not too_complex(_nested(10)) and not too_complex(message())
    assert parse(_nested(10)).body == "hi"


def test_clean_name() -> None:
    assert clean_name("a/b\\c:d*e?.pdf") == "c_d_e_.pdf"
    assert clean_name("...") == "attachment"
    assert clean_name("\x00") == "_"
    long = clean_name("x" * 300 + ".docx")
    assert len(long) == 120 and long.endswith(".docx")
    assert len(clean_name("y" * 300)) == 120
    # 255 bytes at most for a file name: characters of several bytes count as such.
    wide = clean_name("é" * 150 + ".pdf")
    assert len(wide.encode()) <= 200 and wide.endswith(".pdf")
    assert len(clean_name("😀" * 120).encode()) <= 200
    assert len(clean_name("x" * 1_000_000)) == 120  # quickly
    # A right-to-left override would show "fdp.exe" as "exe.pdf".
    assert clean_name("facture\u202efdp.exe") == "facturefdp.exe"


# --- HTML to Markdown -------------------------------------------------------------------------


def test_html_to_markdown_keeps_structure_and_drops_the_dangerous() -> None:
    html = """<html><head><style>p{color:red}</style><title>T</title></head><body>
    <h2>Compte rendu</h2><p>Voir <a href="https://ex.test/a b(c)">le lien</a>,
    <a href="javascript:alert(1)">pas celui-ci</a> et <a href="mailto:a@b.test">écrire</a>.</p>
    <script>document.cookie</script><iframe src="https://evil.test"></iframe>
    <ul><li>un</li><li>deux<ol><li>a</li><li>b</li></ol></li></ul>
    <blockquote><p>cité</p></blockquote>
    <img src="https://track.test/p.gif" width="1" height="1">
    <img src="https://x.test/l.png" alt="Logo">
    <pre>  code
 brut</pre><p>1 &lt; 2 et <code>x*y</code>, <em>it</em><br>suite</p><hr>
    <table><tr><td>a</td><td>b</td></tr></table>
    <svg><text>dessin</text></svg><p>&lt;img src=x onerror=alert(1)&gt;</p></body></html>"""
    text = html_to_markdown(html)
    assert "## Compte rendu" in text
    assert "[le lien](https://ex.test/a%20b%28c%29)" in text
    assert "javascript" not in text and "pas celui-ci" in text
    assert "[écrire](mailto:a@b.test)" in text
    assert "cookie" not in text and "evil.test" not in text and "dessin" not in text
    assert "p{color" not in text and "\nT\n" not in text
    assert "- un\n- deux\n  1. a\n  2. b" in text
    assert "> cité" in text
    assert "track.test" not in text and "https://x.test" not in text and "[image: Logo]" in text
    assert "```\n  code\n brut\n```" in text
    assert "1 \\< 2" in text and "`x\\*y`" in text and "*it*  \nsuite" in text
    assert "---" in text and "a b" in text
    assert "\\<img" in text  # written as text, never as HTML


def test_html_to_markdown_bounds_nested_lists_and_quotes() -> None:
    """Each level of a list or a quote repeats its prefix on the lines inside it: thousands of
    levels in a few kilobytes of HTML would weigh gigabytes of Markdown."""
    lists = html_to_markdown("<ul><li>" * 20_000 + "x")
    assert len(lists) < 500_000
    quotes = html_to_markdown("<blockquote>" * 3000 + "<p>x</p>" * 3000)
    assert len(quotes) < 100_000
    assert html_to_markdown("<blockquote><blockquote>a</blockquote></blockquote>") == "> > a"


def test_html_to_markdown_survives_broken_html() -> None:
    assert html_to_markdown("<p>ouvert <b>gras <i>italique") == "ouvert **gras *italique"
    assert html_to_markdown("</p></div>texte</blockquote>") == "texte"
    assert html_to_markdown("<!DOCTYPE html><![CDATA[x]]><p>ok</p>").endswith("ok")
    assert html_to_markdown("") == ""


def test_defuse_neutralizes_images() -> None:
    assert defuse("![t](https://track.test/p.gif)") == "!\\[t](https://track.test/p.gif)"
    assert defuse('<IMG src="https://t.test">') == '&lt;IMG src="https://t.test">'
    assert defuse("texte [lien](https://ok.test)") == "texte [lien](https://ok.test)"


# --- Routing ----------------------------------------------------------------------------------


def _parsed(**headers: str) -> Parsed:
    mail = EmailMessage()
    mail["From"] = "alice@acme.test"
    for name, value in headers.items():
        mail[name.replace("_", "-")] = value
    mail.set_content("x")
    return parse(mail.as_bytes())


def test_targets_are_read_from_to_and_cc_only() -> None:
    """To and Cc name the spaces (what the sender wrote, signed with the email); the envelope
    only picks, among them, the space of this copy."""
    only_envelope = _parsed(Delivered_To="brain+acme.infra@capture.test", To="team@acme.test")
    assert targets(only_envelope, CAPTURE) == []  # blind copy: aims at nothing
    assert not addressed(only_envelope, CAPTURE)
    this_copy = _parsed(
        Delivered_To="Brain+Acme.Autre@Capture.test",
        To="brain+acme.infra@capture.test",
        Cc="brain+acme.autre@capture.test",
    )
    assert targets(this_copy, CAPTURE) == ["acme.autre"]  # the copy received: case ignored
    elsewhere = _parsed(
        Delivered_To="brain+acme.secret@capture.test", To="brain+acme.infra@capture.test"
    )
    assert targets(elsewhere, CAPTURE) == ["acme.infra"]  # an envelope not in To/Cc: ignored
    several = _parsed(
        To="Équipe <team@acme.test>, brain+acme.infra@capture.test",
        Cc="brain+globex.ventes@capture.test, brain+acme.infra@capture.test",
    )
    assert targets(several, CAPTURE) == ["acme.infra", "globex.ventes"]
    bare = _parsed(To="brain@capture.test")
    assert targets(bare, CAPTURE) == [] and addressed(bare, CAPTURE)  # no space named
    other_domain = _parsed(To="brain+acme.infra@elsewhere.test")
    assert targets(other_domain, CAPTURE) == [] and not addressed(other_domain, CAPTURE)
    prefix = _parsed(To="brainy+acme.infra@capture.test")
    assert targets(prefix, CAPTURE) == []
    many = _parsed(To=", ".join(f"brain+acme.s{i}@capture.test" for i in range(9)))
    assert len(targets(many, CAPTURE)) == 5


@pytest.mark.parametrize("name", SINGLE_HEADERS)
def test_headers_allowed_once_are_reported_when_repeated(name: str) -> None:
    raw = message(
        cc="bob@acme.test", headers={"Sender": "alice@acme.test", "Reply-To": "alice@acme.test"}
    )
    raw = f"{name}: added@evil.test\r\n".encode() + raw
    if name in ("In-Reply-To", "References"):
        raw = f"{name}: <a@x>\r\n".encode() + raw  # twice even when the email has none
    assert name in parse(raw, with_content=False).duplicated
    assert parse(message(), with_content=False).duplicated == ()


def test_received_at_reads_the_topmost_received_header() -> None:
    received = [
        "from mail.acme.test by mx.capture.test with ESMTPS id x; Mon, 9 Nov 2026 10:33:00 +0100",
        "from laptop by mail.acme.test; Mon, 1 Jan 2024 00:00:00 +0000",
    ]
    assert received_at(received) == datetime(2026, 11, 9, 9, 33, tzinfo=UTC)
    assert received_at([]) is None
    assert received_at(["from x by y"]) is None


@pytest.mark.parametrize(
    "tag", ["acme", "acme.infra.x", "acme..infra", ".infra", "ac me.infra", "acme.-x", "a--b.c"]
)
def test_find_space_refuses_malformed_tags_without_a_query(tag: str) -> None:
    class NoSession:
        def scalar(self, *_: object) -> None:
            raise AssertionError("no query for a malformed tag")

    assert find_space(NoSession(), tag) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "line",
    [
        b"Not a header line",  # Python stops reading the headers here, a verifier may not
        b"To : brain+acme.infra@capture.test",  # obsolete syntax: read by some, not by Python
        b"X-Odd\x00: y",
        b"From alice@acme.test Mon Nov  9 09:00:00 2026",  # a second envelope line
    ],
)
def test_a_line_of_the_headers_that_python_does_not_read_is_reported(line: bytes) -> None:
    """Headers of one's own, then such a line, then a signed email: Python would read only the
    added headers and see no duplicate, where a DKIM verifier reads on to the signed ones."""
    raw = b"From: alice@acme.test\r\nSubject: added\r\n" + line + b"\r\n" + message()
    assert parse(raw, with_content=False).broken_headers
    assert not parse(message(), with_content=False).broken_headers
