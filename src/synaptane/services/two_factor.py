"""Two-factor authentication: a code from an authenticator app after the password.

Turned on by each person in My account → Security (a QR code to scan, then a code to confirm
it), with ten single-use recovery codes for a lost phone. Required for super admins who sign
in with a password (`superadmin_2fa`); single sign-on leaves it to the identity provider.
Personal tokens and OAuth connections (assistants) do not ask for it: they are created from a
session that did.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core import totp
from synaptane.core.errors import forbidden, invalid
from synaptane.core.security import SecretBox, token_hash
from synaptane.services import Actor, audit
from synaptane.storage.models import User

ISSUER = "Synaptane"


def _context(user: User) -> str:
    return f"totp:{user.id}"


def enabled(user: User) -> bool:
    return user.totp_enabled_at is not None


def required(settings: Settings, user: User) -> bool:
    """A super admin who signs in with a password must turn it on before anything else."""
    return settings.superadmin_2fa and user.is_superadmin and user.password_hash is not None


def setup_uri(user: User, secret: str) -> str:
    return totp.uri(secret, user.username, ISSUER)


def _recovery_digests(codes: list[str]) -> list[str]:
    return [token_hash(totp.recovery_key(code)) for code in codes]


def enable(session: Session, box: SecretBox, actor: Actor, secret: str, code: str) -> list[str]:
    """Turns two-factor authentication on with `secret` once `code` shows the app has it;
    returns the recovery codes, to show only once."""
    user = actor.user
    if enabled(user):
        raise invalid("Two-factor authentication is already on.")
    step = totp.check(secret, code)
    if step is None:
        raise invalid("Incorrect code: type the one your app shows now.")
    codes = totp.new_recovery_codes()
    user.totp_secret = box.encrypt(secret, _context(user))
    user.totp_enabled_at = datetime.now(UTC)
    user.totp_step = step
    user.recovery_codes = _recovery_digests(codes)
    audit(session, actor, "user.2fa_enabled")
    return codes


def verify(session: Session, box: SecretBox, user: User, code: str) -> bool:
    """A code of the app (used once), or one of the recovery codes (then spent)."""
    if not enabled(user) or user.totp_secret is None:
        return False
    secret = box.decrypt(user.totp_secret, _context(user))
    step = totp.check(secret, code, after=user.totp_step)
    if step is not None:
        user.totp_step = step
        return True
    digest = token_hash(totp.recovery_key(code))
    left = list(user.recovery_codes or [])
    if totp.recovery_key(code) and digest in left:
        left.remove(digest)
        user.recovery_codes = left
        audit(session, None, "user.recovery_code_used", username=user.username, left=len(left))
        return True
    return False


def recovery_left(user: User) -> int:
    return len(user.recovery_codes or [])


def _confirmed(session: Session, box: SecretBox, user: User, code: str) -> None:
    if not verify(session, box, user, code):
        raise invalid("Incorrect code: type the one your app shows now, or a recovery code.")


def renew_recovery_codes(session: Session, box: SecretBox, actor: Actor, code: str) -> list[str]:
    """New recovery codes (the old ones stop working), after a code."""
    _confirmed(session, box, actor.user, code)
    codes = totp.new_recovery_codes()
    actor.user.recovery_codes = _recovery_digests(codes)
    audit(session, actor, "user.recovery_codes_renewed")
    return codes


def _clear(user: User) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_step = None
    user.recovery_codes = None


def disable(session: Session, box: SecretBox, settings: Settings, actor: Actor, code: str) -> None:
    """Turns it off, after a code; not for a super admin who must keep it."""
    if required(settings, actor.user):
        raise forbidden("Super admins must keep two-factor authentication.")
    _confirmed(session, box, actor.user, code)
    _clear(actor.user)
    audit(session, actor, "user.2fa_disabled")


def reset(session: Session, actor: Actor | None, user: User) -> None:
    """A super admin turns it off for someone who lost their phone and their recovery codes:
    they sign in with their password alone, then turn it on again. `actor` None: the command
    line, on the server (the last super admin locked out)."""
    if actor is not None and not actor.access.is_superadmin:
        raise forbidden("Only a super admin can do this.")
    _clear(user)
    audit(session, actor, "user.2fa_reset", username=user.username)
