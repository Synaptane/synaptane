"""Passwords, opaque tokens and encryption of secrets at rest."""

import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from synaptane.core.errors import invalid

_hasher = PasswordHasher()
# Dummy hash: an unknown username costs as much as a wrong password.
_DUMMY_HASH = _hasher.hash("synaptane-dummy-password")

MIN_PASSWORD_LENGTH = 12
TOKEN_PREFIX = "syn_"


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > 256:
        raise invalid(
            "The password must be between {min} and 256 characters long.", min=MIN_PASSWORD_LENGTH
        )
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(validate_password(password))


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def new_token(prefix: str = TOKEN_PREFIX) -> str:
    """Opaque token shown only once; only its hash is stored."""
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class SecretBox:
    """AES-256-GCM with a master key stored outside the database. The context binds the
    ciphertext to its use."""

    KEY_BYTES = 32

    def __init__(self, key: bytes) -> None:
        if len(key) != self.KEY_BYTES:
            raise ValueError("the master key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def generate_key(cls) -> bytes:
        return os.urandom(cls.KEY_BYTES)

    def encrypt(self, plaintext: str, context: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), context.encode())

    def decrypt(self, ciphertext: bytes, context: str) -> str:
        return self._aead.decrypt(ciphertext[:12], ciphertext[12:], context.encode()).decode()
