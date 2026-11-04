import base64

from synaptane.core import totp

# RFC 6238, appendix B: the SHA-1 secret "12345678901234567890" (the last 6 of its 8 digits).
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_codes_of_the_rfc() -> None:
    for seconds, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924")):
        assert totp.code_at(RFC_SECRET, totp.step_now(seconds)) == code


def test_a_code_is_valid_half_a_minute_around_its_time_and_only_once() -> None:
    now = 1_800_000_000.0
    step = totp.step_now(now)
    code = totp.code_at(RFC_SECRET, step)
    assert totp.check(RFC_SECRET, code, now=now) == step
    assert totp.check(RFC_SECRET, f" {code[:3]} {code[3:]} ", now=now + 30) == step  # spaces
    assert totp.check(RFC_SECRET, code, now=now + 61) is None  # too late
    assert totp.check(RFC_SECRET, code, after=step, now=now) is None  # already used
    assert totp.check(RFC_SECRET, "12345", now=now) is None
    assert totp.check(RFC_SECRET, "abcdef", now=now) is None


def test_a_new_secret_and_its_address() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32 and "=" not in secret
    address = totp.uri(secret, "alice", "Synaptane")
    assert address.startswith("otpauth://totp/Synaptane%3Aalice?secret=" + secret)
    assert "issuer=Synaptane" in address and "digits=6" in address and "period=30" in address


def test_recovery_codes() -> None:
    codes = totp.new_recovery_codes()
    assert len(codes) == len(set(codes)) == totp.RECOVERY_CODES
    assert all(len(code) == 11 and code[5] == "-" for code in codes)
    assert not set("".join(codes)) & set("01ilo")  # nothing to confuse
    assert totp.recovery_key(" AbCdE-fGhJk ") == totp.recovery_key("abcdefghjk") == "abcdefghjk"
