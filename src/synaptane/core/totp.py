"""One-time codes of an authenticator app (TOTP, RFC 6238), and recovery codes.

The codes are those of Google Authenticator, Aegis, 1Password, Bitwarden and the others: six
digits, renewed every 30 seconds, computed with HMAC-SHA1 from a secret shared once through a
QR code. A code is accepted 30 seconds before or after its time (clocks drift), and only once.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30  # seconds
DRIFT = 1  # periods accepted before and after the current one
RECOVERY_CODES = 10


def new_secret() -> str:
    """A secret of 160 bits, in base32 without padding, as authenticator apps expect it."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    secret = secret.replace(" ", "").upper()
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def code_at(secret: str, step: int) -> str:
    """The code of the time step `step` (RFC 4226 truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def step_now(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def check(secret: str, code: str, after: int | None = None, now: float | None = None) -> int | None:
    """The time step of `code` if it is valid now (give or take `DRIFT`), None otherwise.
    `after`: the step of the last code accepted, refused again (a code is used once)."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = step_now(now)
    for step in range(current - DRIFT, current + DRIFT + 1):
        if after is not None and step <= after:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str, issuer: str) -> str:
    """The otpauth:// address an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes() -> list[str]:
    """Single-use codes for when the phone is lost: `xxxxx-xxxxx`, letters and digits that
    cannot be confused (no 0, O, 1, l, I)."""
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2))
        for _ in range(RECOVERY_CODES)
    ]


def recovery_key(code: str) -> str:
    """Normalized form of a recovery code, as typed (spaces, capitals, without the dash)."""
    return "".join(c for c in code.lower() if c.isalnum())
