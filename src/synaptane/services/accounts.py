"""Accounts, web sessions and personal tokens."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, Limits, OrgRole, require
from synaptane.core.errors import (
    SynaptaneError,
    conflict,
    forbidden,
    invalid,
    not_found,
    too_many,
)
from synaptane.core.security import hash_password, new_token, token_hash, verify_password
from synaptane.services import Actor, audit
from synaptane.storage.models import (
    ApiToken,
    EmailCheck,
    OAuthToken,
    OrgMember,
    PasswordReset,
    Space,
    User,
    WebSession,
)

LOCALES = ("fr", "en")


def _now() -> datetime:
    return datetime.now(UTC)


def create_user(
    session: Session,
    actor: Actor | None,
    *,
    username: str,
    display_name: str,
    password: str | None,
    email: str | None = None,
    is_superadmin: bool = False,
    locale: str,
) -> User:
    """`actor=None` is reserved for the CLI (creation of the first super-admin) and for
    accounts created from an invitation. `locale`: initial language of the account (that of
    the person's browser when available, otherwise the instance's default language)."""
    if actor is not None:
        require(actor.access, Action.MANAGE_INSTANCE)
    if locale not in LOCALES:
        raise invalid("Unsupported language.")
    username = names.username(username)
    if session.scalar(select(User).where(User.username == username)):
        raise conflict("The username “{username}” is already taken.", username=username)
    email = (email or "").strip().lower() or None
    if email:
        claim_email(session, email)
    user = User(
        username=username,
        display_name=names.display_name(display_name),
        email=email,
        password_hash=hash_password(password) if password else None,
        is_superadmin=is_superadmin,
        locale=locale,
        # Address entered through the CLI (first super-admin, on the server): trusted.
        # Entered by an admin: to be verified by the person (link sent to that address).
        email_verified_at=_now() if email and actor is None else None,
        # Password chosen by an admin: the person changes it at first login.
        must_change_password=actor is not None and bool(password),
    )
    session.add(user)
    session.flush()
    audit(session, actor, "user.created", username=username, superadmin=is_superadmin)
    return user


def get_user(session: Session, username: str) -> User:
    user = session.scalar(select(User).where(User.username == username.lower()))
    if user is None:
        raise not_found("Account not found.")
    return user


def list_users(session: Session, actor: Actor) -> list[User]:
    require(actor.access, Action.MANAGE_INSTANCE)
    return list(session.scalars(select(User).order_by(User.username)))


def directory(session: Session, actor: Actor) -> list[tuple[str, str]]:
    """Active accounts (username, name) offered when adding a member, for admins."""
    access = actor.access
    if not (access.is_superadmin or OrgRole.ADMIN in access.org_roles.values()):
        raise forbidden("Action reserved for administrators.")
    query = select(User).where(User.is_active.is_(True)).order_by(User.username)
    if not access.is_superadmin:
        # An organization admin only sees the members of their organizations: for others,
        # they invite by email (the instance directory is not public).
        admin_orgs = [org for org, role in access.org_roles.items() if role is OrgRole.ADMIN]
        query = query.where(
            User.id.in_(select(OrgMember.user_id).where(OrgMember.org_id.in_(admin_orgs)))
        )
    return [(u.username, u.display_name) for u in session.scalars(query)]


def unrestricted(actor: Actor) -> None:
    """Managing the account (tokens, password, email) requires full access."""
    if actor.restricted:
        raise forbidden("This token is restricted: it cannot manage the account.")


def claim_email(session: Session, email: str, owner: User | None = None) -> None:
    """Makes `email` available to `owner` (or a new account).

    An address verified by another account is refused. An address only declared (never
    verified) by another account is taken away from it: otherwise, anyone could block someone
    else's address."""
    other = session.scalar(select(User).where(User.email == email))
    if other is None or (owner is not None and other.id == owner.id):
        return
    if other.email_verified_at is not None:
        raise conflict("This email address is already in use.")
    other.email, other.email_verified_at = None, None
    audit(session, None, "user.unverified_email_released", username=other.username)
    session.flush()


def update_user(
    session: Session,
    actor: Actor,
    user: User,
    *,
    display_name: str | None = None,
    email: str | None = None,
    locale: str | None = None,
    is_active: bool | None = None,
    is_superadmin: bool | None = None,
    current_password: str | None = None,
) -> User:
    """A new email stays "to verify" until the link (`start_email_check`). Changing one's own
    requires the current password (if there is one)."""
    unrestricted(actor)
    own = user.id == actor.user.id
    if not own or is_active is not None or is_superadmin is not None:
        require(actor.access, Action.MANAGE_INSTANCE)
    if own and (is_active is False or is_superadmin is False):
        raise invalid("You cannot remove your own permissions or deactivate yourself.")
    if display_name is not None:
        user.display_name = names.display_name(display_name)
    if email is not None:
        email = email.strip().lower() or None
        if email != user.email:
            if (
                own
                and user.password_hash
                and not verify_password(user.password_hash, current_password or "")
            ):
                raise forbidden("Enter your current password to change the email address.")
            if email:
                if "@" not in email or len(email) > 320 or " " in email:
                    raise invalid("Invalid email address.")
                claim_email(session, email, user)
            user.email, user.email_verified_at = email, None
    if locale is not None:
        if locale not in LOCALES:
            raise invalid("Unsupported language.")
        user.locale = locale
    if is_active is not None:
        user.is_active = is_active
        if not is_active:
            _revoke_everything(session, user.id)
    if is_superadmin is not None:
        user.is_superadmin = is_superadmin
    audit(session, actor, "user.updated", username=user.username)
    return user


def set_tidy_notices(session: Session, actor: Actor, enabled: bool) -> None:
    """Email notices when the tidy run processes one's notes (`services.tidy_notices`)."""
    unrestricted(actor)
    actor.user.notify_tidy = enabled
    audit(session, actor, "user.tidy_notices", enabled=enabled)


def set_routine_notices(session: Session, actor: Actor, enabled: bool) -> None:
    """Emails from routines that write to the team (`services.routine_agent.send_report`):
    a separate preference, a team summary is not a notice about one's own notes."""
    unrestricted(actor)
    actor.user.notify_routines = enabled
    audit(session, actor, "user.routine_notices", enabled=enabled)


def set_password(
    session: Session, actor: Actor, user: User, new_password: str, current: str | None = None
) -> None:
    unrestricted(actor)
    if user.id == actor.user.id:
        # An account created through SSO may set a first password without an old one.
        if user.password_hash and not verify_password(user.password_hash, current or ""):
            raise forbidden("Current password is incorrect.")
    else:
        require(actor.access, Action.MANAGE_INSTANCE)
    user.password_hash = hash_password(new_password)
    user.must_change_password = user.id != actor.user.id  # reset by an admin
    _revoke_everything(session, user.id)
    _expire_resets(session, user.id)
    audit(session, actor, "user.password_changed", username=user.username)


def find_login(session: Session, login: str) -> User | None:
    """Account designated by its username or by its email address."""
    login = login.strip()
    if "@" in login:
        return session.scalar(select(User).where(User.email == login.lower()))
    try:
        username = names.username(login)
    except SynaptaneError:
        return None
    return session.scalar(select(User).where(User.username == username))


def login_key(session: Session, login: str) -> str:
    """Login rate-limiting key: the designated account, however the login is written (spaces,
    capitals, email or name); otherwise a short digest of the normalized login. It ends up in
    the log: never the typed text, which may be a password typed in the wrong field."""
    user = find_login(session, login)
    if user is not None:
        return f"id:{user.id}"
    return "login:" + hashlib.sha256(login.strip().lower().encode()).hexdigest()[:8]


def authenticate(session: Session, username: str, password: str) -> User | None:
    """`username`: username or email address."""
    user = find_login(session, username)
    if not verify_password(user.password_hash if user else None, password):
        return None
    return user if user and user.is_active else None


def _revoke_everything(session: Session, user_id: UUID) -> None:
    """Closes web sessions, personal tokens and OAuth connections (MCP applications)."""
    session.execute(delete(WebSession).where(WebSession.user_id == user_id))
    session.execute(
        update(OAuthToken)
        .where(OAuthToken.user_id == user_id, OAuthToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    session.execute(
        update(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


# --- Web sessions -----------------------------------------------------------------------------


def open_web_session(session: Session, user: User, days: int) -> tuple[str, WebSession]:
    token = new_token("sess_")
    web_session = WebSession(
        token_hash=token_hash(token),
        csrf_token=new_token("csrf_"),
        user_id=user.id,
        expires_at=_now() + timedelta(days=days),
    )
    session.add(web_session)
    session.flush()
    return token, web_session


def resolve_web_session(session: Session, token: str) -> tuple[User, WebSession] | None:
    web_session = session.scalar(
        select(WebSession).where(WebSession.token_hash == token_hash(token))
    )
    if web_session is None or web_session.expires_at <= _now():
        return None
    user = session.get(User, web_session.user_id)
    if user is None or not user.is_active:
        return None
    return user, web_session


def close_web_session(session: Session, token: str) -> None:
    session.execute(delete(WebSession).where(WebSession.token_hash == token_hash(token)))


# --- Personal tokens --------------------------------------------------------------------------


def checked_spaces(
    session: Session, actor: Actor, space_ids: list[UUID] | None
) -> list[str] | None:
    """Spaces chosen for a token, among those the account is a member of (or admin of the
    organization): a restricted token never has the super-admin's powers. None: all."""
    if space_ids is None:
        return None
    if not space_ids:
        raise invalid("Choose at least one space, or “all spaces”.")
    for space_id in space_ids:
        space = session.get(Space, space_id)
        if space is None or not (
            space.id in actor.access.space_roles
            or actor.access.org_roles.get(space.org_id) is OrgRole.ADMIN
        ):
            raise invalid("Unknown space, or one you are not a member of.")
    return sorted({str(s) for s in space_ids})


def create_api_token(
    session: Session,
    actor: Actor,
    name: str,
    days: int | None = 90,
    *,
    read_only: bool = False,
    space_ids: list[UUID] | None = None,
    inbox_only: bool = False,
) -> tuple[str, ApiToken]:
    """Personal token, optionally restricted: read-only, or reading and the inbox only (no
    direct change to pages), chosen spaces."""
    if read_only and inbox_only:
        raise invalid("Choose either read only or the inbox only.")
    unrestricted(actor)
    if days is not None and not 1 <= days <= 3650:
        raise invalid("Validity: between 1 and 3650 days.")
    token = new_token()
    api_token = ApiToken(
        user_id=actor.user.id,
        name=names.display_name(name, field="Token name"),
        token_hash=token_hash(token),
        hint=token[-4:],
        read_only=read_only,
        inbox_only=inbox_only,
        space_ids=checked_spaces(session, actor, space_ids),
        expires_at=_now() + timedelta(days=days) if days else None,
    )
    session.add(api_token)
    session.flush()
    audit(
        session,
        actor,
        "token.created",
        name=api_token.name,
        read_only=read_only,
        inbox_only=inbox_only,
    )
    return token, api_token


def list_api_tokens(session: Session, actor: Actor) -> list[ApiToken]:
    return list(
        session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == actor.user.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
    )


def revoke_api_token(session: Session, actor: Actor, token_id: UUID) -> None:
    unrestricted(actor)
    api_token = session.get(ApiToken, token_id)
    if api_token is None or api_token.user_id != actor.user.id or api_token.revoked_at:
        raise not_found("Token not found.")
    api_token.revoked_at = _now()
    audit(session, actor, "token.revoked", name=api_token.name)


def token_limits(
    read_only: bool, space_ids: list[str] | None, inbox_only: bool = False, **extra: bool
) -> Limits:
    return Limits(
        read_only=read_only,
        space_ids=frozenset(UUID(s) for s in space_ids) if space_ids is not None else None,
        inbox_only=inbox_only and not read_only,
        **extra,
    )


def resolve_api_token(session: Session, token: str) -> tuple[User, ApiToken] | None:
    """Account and token matching a valid personal token."""
    api_token = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(token)))
    now = _now()
    if (
        api_token is None
        or api_token.revoked_at is not None
        or (api_token.expires_at is not None and api_token.expires_at <= now)
    ):
        return None
    user = session.get(User, api_token.user_id)
    if user is None or not user.is_active:
        return None
    if api_token.last_used_at is None or now - api_token.last_used_at > timedelta(minutes=5):
        api_token.last_used_at = now
    return user, api_token


# --- Forgotten password -----------------------------------------------------------------------

RESET_TTL = timedelta(minutes=30)


def request_password_reset(session: Session, login: str) -> tuple[User, str] | None:
    """Reset link for this account (None if unknown, inactive or without email).

    The caller always answers the same thing, whether the account exists or not."""
    user = find_login(session, login)
    if user is None or not user.is_active or not user.email or user.email_verified_at is None:
        return None  # never to an unverified address
    token = new_token("reset_")
    session.add(
        PasswordReset(token_hash=token_hash(token), user_id=user.id, expires_at=_now() + RESET_TTL)
    )
    audit(session, None, "user.password_reset_requested", username=user.username)
    return user, token


def _expire_resets(session: Session, user_id: UUID) -> None:
    """Password changed: the other "forgotten password" links are no longer valid."""
    session.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
        .values(used_at=_now())
    )


def reset_target(session: Session, token: str) -> User:
    reset = session.get(PasswordReset, token_hash(token))
    if reset is None or reset.used_at is not None or reset.expires_at < _now():
        raise invalid("This link is no longer valid: ask for a new one.")
    user = session.get(User, reset.user_id)
    if user is None or not user.is_active:
        raise invalid("This link is no longer valid: ask for a new one.")
    return user


def reset_password(session: Session, token: str, new_password: str) -> User:
    user = reset_target(session, token)
    reset = session.get(PasswordReset, token_hash(token))
    assert reset is not None
    reset.used_at = _now()
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    _revoke_everything(session, user.id)
    _expire_resets(session, user.id)
    audit(session, None, "user.password_reset", username=user.username)
    return user


# --- Email verification ----------------------------------------------------------------------

EMAIL_CHECK_TTL = timedelta(days=3)
# Verification links per account and per hour: each one goes to an address chosen by the
# account, which must not be able to turn it into an email cannon.
EMAIL_CHECKS_PER_HOUR = 3


def email_checks_left(session: Session, user: User) -> int:
    """Verification links the account may still request within the hour. Counts existing
    links (never deleted): created less than an hour ago, they expire in more than
    `EMAIL_CHECK_TTL` minus one hour."""
    since = _now() + EMAIL_CHECK_TTL - timedelta(hours=1)
    sent = session.scalar(
        select(func.count())
        .select_from(EmailCheck)
        .where(EmailCheck.user_id == user.id, EmailCheck.expires_at > since)
    )
    return max(0, EMAIL_CHECKS_PER_HOUR - (sent or 0))


def check_email_quota(session: Session, user: User) -> None:
    if email_checks_left(session, user) == 0:
        raise too_many("Too many verification links sent in the last hour: try again later.")


def start_email_check(session: Session, user: User) -> str:
    """Verification link for the account's current address (to send to that address),
    within the limit of `EMAIL_CHECKS_PER_HOUR`."""
    if not user.email:
        raise invalid("This account has no email address.")
    check_email_quota(session, user)
    token = new_token("mail_")
    session.add(
        EmailCheck(
            token_hash=token_hash(token),
            user_id=user.id,
            email=user.email,
            expires_at=_now() + EMAIL_CHECK_TTL,
        )
    )
    return token


def email_check_target(session: Session, token: str) -> tuple[EmailCheck, User]:
    """Link still valid (address unchanged since sending) and account that requested it."""
    check = session.get(EmailCheck, token_hash(token))
    user = session.get(User, check.user_id) if check else None
    if (
        check is None
        or check.used_at is not None
        or check.expires_at < _now()
        or user is None
        or not user.is_active
        or user.email != check.email
    ):
        raise invalid("This link is no longer valid: ask for a new one from “My account”.")
    return check, user


def confirm_email(session: Session, token: str, user: User) -> User:
    """Marks the address as verified, for the logged-in account that requested the link.

    Receiving the link is not enough: anyone can enter someone else's address on their account,
    and the person opening the link (or their mail antivirus) would then verify the address for
    that account, which would capture their invitations and SSO login."""
    check, owner = email_check_target(session, token)
    if owner.id != user.id:
        raise forbidden(
            "This link is for another account: sign in with the account that requested it."
        )
    check.used_at = _now()
    user.email_verified_at = _now()
    audit(session, None, "user.email_verified", username=user.username)
    return user
