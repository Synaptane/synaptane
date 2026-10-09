"""Encrypted secrets of a space (Git credentials, webhook secret…) or of an organization.

The encryption context binds each ciphertext to its owner and purpose: a ciphertext copied
to another space or another purpose does not decrypt.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.security import SecretBox
from synaptane.storage.models import Secret, utcnow


def _context(space_id: UUID, purpose: str) -> str:
    return f"space:{space_id}:{purpose}"


def put_space_secret(
    session: Session,
    box: SecretBox,
    space_id: UUID,
    purpose: str,
    value: str,
    updated_by: UUID | None,
) -> None:
    secret = session.scalar(
        select(Secret).where(Secret.space_id == space_id, Secret.purpose == purpose)
    )
    if secret is None:
        secret = Secret(space_id=space_id, purpose=purpose, ciphertext=b"")
        session.add(secret)
    secret.ciphertext = box.encrypt(value, _context(space_id, purpose))
    secret.updated_by, secret.updated_at = updated_by, utcnow()


def get_space_secret(session: Session, box: SecretBox, space_id: UUID, purpose: str) -> str | None:
    secret = session.scalar(
        select(Secret).where(Secret.space_id == space_id, Secret.purpose == purpose)
    )
    return box.decrypt(secret.ciphertext, _context(space_id, purpose)) if secret else None


def delete_space_secret(session: Session, space_id: UUID, purpose: str) -> bool:
    secret = session.scalar(
        select(Secret).where(Secret.space_id == space_id, Secret.purpose == purpose)
    )
    if secret is None:
        return False
    session.delete(secret)
    return True


def _org_context(org_id: UUID, purpose: str) -> str:
    return f"org:{org_id}:{purpose}"


def put_org_secret(
    session: Session,
    box: SecretBox,
    org_id: UUID,
    purpose: str,
    value: str,
    updated_by: UUID | None,
) -> None:
    secret = session.scalar(
        select(Secret).where(Secret.org_id == org_id, Secret.purpose == purpose)
    )
    if secret is None:
        secret = Secret(org_id=org_id, purpose=purpose, ciphertext=b"")
        session.add(secret)
    secret.ciphertext = box.encrypt(value, _org_context(org_id, purpose))
    secret.updated_by, secret.updated_at = updated_by, utcnow()


def get_org_secret(session: Session, box: SecretBox, org_id: UUID, purpose: str) -> str | None:
    secret = session.scalar(
        select(Secret).where(Secret.org_id == org_id, Secret.purpose == purpose)
    )
    return box.decrypt(secret.ciphertext, _org_context(org_id, purpose)) if secret else None


def readable(session: Session, box: SecretBox) -> bool | None:
    """Do the stored secrets decrypt with the current master key? None: there are none.

    A replaced key (restore without the original key) makes every secret unreadable."""
    secret = session.scalar(select(Secret).limit(1))
    if secret is None:
        return None
    try:
        if secret.space_id is not None:
            box.decrypt(secret.ciphertext, _context(secret.space_id, secret.purpose))
        elif secret.org_id is not None:
            box.decrypt(secret.ciphertext, _org_context(secret.org_id, secret.purpose))
    except Exception:  # noqa: BLE001 — any decryption failure counts
        return False
    return True


CUSTOM_URL_REFUSED = (
    "Only the super-admin can change an API's address: it would make the server call any "
    "address, internal ones included."
)
KEY_REQUIRED = (
    "Enter the API key again: the provider or the address changed, and a saved key is never "
    "sent to any destination other than its own."
)


def check_destination(
    *,
    superadmin: bool,
    old: tuple[str, str] | None,
    new: tuple[str, str],
    has_key: bool,
    key_given: bool,
) -> str | None:
    """Rule shared by AI and embedding keys: (provider, address) before and after. Returns
    the refusal message, or None."""
    changed_url = new[1] != (old[1] if old else "")
    if changed_url and new[1] and not superadmin:
        return CUSTOM_URL_REFUSED
    if old is not None and old != new and has_key and not key_given:
        return KEY_REQUIRED
    return None
