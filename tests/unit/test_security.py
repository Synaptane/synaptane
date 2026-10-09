import pytest
from cryptography.exceptions import InvalidTag

from synaptane.core.errors import SynaptaneError
from synaptane.core.security import (
    SecretBox,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)


def test_password_roundtrip_and_rejections() -> None:
    hashed = hash_password("correct horse battery")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong password!!")
    assert not verify_password(None, "synaptane-dummy-password")
    assert not verify_password("not-a-hash", "whatever")


def test_short_password_is_refused() -> None:
    with pytest.raises(SynaptaneError):
        hash_password("short")


def test_tokens_are_random_and_hashed() -> None:
    a, b = new_token(), new_token()
    assert a.startswith("syn_") and a != b
    assert token_hash(a) != a and len(token_hash(a)) == 64


def test_secret_box_binds_ciphertext_to_its_context() -> None:
    box = SecretBox(SecretBox.generate_key())
    sealed = box.encrypt("glpat-secret", "space:1:git")
    assert b"glpat-secret" not in sealed
    assert box.decrypt(sealed, "space:1:git") == "glpat-secret"
    with pytest.raises(InvalidTag):
        box.decrypt(sealed, "space:2:git")
    with pytest.raises(InvalidTag):
        SecretBox(SecretBox.generate_key()).decrypt(sealed, "space:1:git")
