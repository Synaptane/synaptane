"""Secret detection before a commit: a secret pushed to the Git host is costly to erase.

Assistants drop infrastructure notes; a pasted password or API key would go straight into
Git. The patterns aim for precision (known key formats, private keys, credentials in a URL,
`password = …` assignments whose value looks like a secret): a false positive is cleared by
confirming, a missed secret cannot be taken back.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from synaptane.core.errors import ErrorCode, Label, SynaptaneError

MAX_SCAN = 2_000_000  # bytes scanned per text


@dataclass(frozen=True)
class Finding:
    kind: str  # interface text, to translate ("private key")
    line: int
    where: str = ""  # file or field concerned (a field: `Label`, to translate)


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Stripe key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("Google key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Synaptane token", re.compile(r"\bsyn_[A-Za-z0-9_-]{30,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    (
        "JWT token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
]

# Secret keyword, including when glued to a prefix (`DB_PASSWORD`) or formatted in Markdown
# (`**Mot de passe** :`, table cell). The words that follow (up to WINDOW characters:
# "root est", "Wi-Fi :"…) are examined one by one.
_KEYWORD = re.compile(
    r"(?i)(?<![a-z0-9])(?:password|passwd|pwd|passphrase|mots?[ _-]?de[ _-]?passe|mdp|secret|"
    r"api[_ -]?key|cl[ée]s?[ _-](?:d['’]?\s?)?api|access[_ -]?key|client[_ -]?secret|"
    r"private[_ -]?key|token|jeton)s?(?![a-z0-9])"
)
WINDOW = 60
_EDGES = "`*\"'“”«»|:=,;()[]{}<>"
_PROSE_SLUG = re.compile(r"^[^\W\d_]+(?:-[^\W_]+){2,}$")  # "change-it-every-90-days"
_COMPOUND = re.compile(r"^[^\W\d_]+(?:-[^\W\d_]+)+$")  # "multi-cloud", "self-hosted"
_AMOUNT = re.compile(r"^[\d\s.,]+\s?(?:[kKmM]?[€$£%]|euros?|eur|usd)(?:\s?(?:HT|TTC))?(?:/\w+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?$")
_SENTENCE_MARKS = set(".,;:!?()/-'’")
# Credentials in a URL: scheme://user:password@host
_URL_CREDENTIALS = re.compile(
    r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,64}:(?P<value>[^\s/@]{3,128})@[\w.-]+"
)
_PLACEHOLDERS = re.compile(
    r"(?i)^(?:<[^>]*>|\$\{?\w+\}?|\{\{.*\}\}|%\w+%|\*+|x+|\.+|-+|_+|"
    r"password|motdepasse|mot-de-passe|secret|changeme|example|exemple|redacted|"
    r"masqué|caché|hidden|none|null|true|false|xxx+.*|\.\.\..*)$"
)
_SECRET_FILES = re.compile(
    r"(?i)^(?:id_(?:rsa|dsa|ecdsa|ed25519)|\.env(?:\..+)?|.*\.(?:p12|pfx|jks|keystore))$"
)


def _looks_secret(value: str) -> bool:
    """Does an assigned value look like a secret (and not like a word of the language)?

    Rules out words with an apostrophe ("l'expérience"), compound words ("multi-cloud"),
    amounts ("80€/mois") and dates. Without a digit, the value must be long and mix
    uppercase, lowercase and symbols other than punctuation."""
    value = value.strip().rstrip(".")
    if len(value) < 8 or _PLACEHOLDERS.match(value):
        return False
    if "'" in value or "’" in value or _COMPOUND.match(value):
        return False
    if _AMOUNT.match(value) or _DATE.match(value):
        return False
    if not any(c.isalpha() for c in value):
        return False
    if any(c.isdigit() for c in value):
        return True
    symbols = {c for c in value if not c.isalnum()} - _SENTENCE_MARKS
    return (
        len(value) >= 12
        and bool(symbols)
        and any(c.isupper() for c in value)
        and any(c.islower() for c in value)
    )


def _keyword_value(line: str) -> bool:
    """Is a secret keyword closely followed by a value that looks like a secret?"""
    for match in _KEYWORD.finditer(line):
        after = line[match.end() : match.end() + WINDOW]
        if after.startswith("@"):  # user:password@host: the keyword is the value
            continue
        for raw in re.split(r"[\s|]+", after):
            value = raw.strip(_EDGES)
            if "://" in value or value.startswith(("/", "../", "./")) or _PROSE_SLUG.match(value):
                continue  # address, path, hyphenated phrase
            if _looks_secret(value):
                return True
    return False


def scan_text(text: str, where: str = "") -> list[Finding]:
    found: list[Finding] = []
    text = text[:MAX_SCAN]
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _PATTERNS:
            if pattern.search(line):
                found.append(Finding(kind, number, where))
        if _keyword_value(line):
            found.append(Finding("password or key", number, where))
        for match in _URL_CREDENTIALS.finditer(line):
            if not _PLACEHOLDERS.match(match.group("value")):
                found.append(Finding("credentials in a URL", number, where))
    return found


def scan_file(name: str, data: bytes) -> list[Finding]:
    """File: its name (private key, .env…), then its content if it is text."""
    found = []
    if _SECRET_FILES.match(PurePosixPath(name).name):
        found.append(Finding("secrets file", 0, name))
    if b"\0" in data[:8192]:
        return found
    try:
        data[:MAX_SCAN].decode("utf-8")
    except UnicodeDecodeError as error:
        # Cut in the middle of a character by the limit: the text is still scanned.
        if error.start < MAX_SCAN - 4:
            return found
    return found + scan_text(data[:MAX_SCAN].decode("utf-8", "ignore"), name)


def added_findings(name: str, new: bytes, old: bytes | None) -> list[Finding]:
    """Secrets in added lines only: a page that already contained some stays editable."""
    findings = scan_file(name, new)
    if old is None or not findings:
        return findings
    try:
        before = set(old.decode("utf-8").splitlines())
        lines = new.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return findings
    return [f for f in findings if f.line and lines[f.line - 1] not in before]


def describe(findings: list[Finding]) -> Label:
    """ "private key (notes.md, line 3), …": a text to translate, made of the findings."""
    parts: dict[str, object] = {}
    for index, f in enumerate(findings[:5]):
        if f.where and f.line:
            place = Label("{where}, line {line}", where=f.where, line=f.line)
        elif f.where:
            place = f.where if isinstance(f.where, Label) else Label("{where}", where=f.where)
        else:
            place = Label("line {line}", line=f.line)
        parts[f"p{index}"] = Label("{kind} ({place})", kind=Label(f.kind), place=place)
    template = ", ".join(f"{{{name}}}" for name in parts)
    if len(findings) > 5:
        parts["more"] = Label("and {count} more", count=len(findings) - 5)
        template += " {more}"
    return Label(template, **parts)


def refuse_secrets(findings: list[Finding]) -> None:
    """Refuse the write if a secret is detected. The caller can confirm and retry."""
    if findings:
        raise SynaptaneError(
            ErrorCode.SECRET,
            "This content seems to contain a secret: {detail}. It would be pushed to Git and to "
            "the Git host. Remove it and keep it in a password manager. If it is not one, "
            "confirm to save.",
            detail=describe(findings),
        )
