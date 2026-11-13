"""Cryptographic MFA boundaries; test secrets are synthetic."""

import re

import pytest

from preloop.services import mfa


@pytest.mark.parametrize(
    "offset,accepted", [(-2, False), (-1, True), (0, True), (1, True), (2, False)]
)
def test_totp_window(offset: int, accepted: bool) -> None:
    """protects=exact three-step window; fails_when=outside codes pass;
    no_claim=database replay locking is covered separately."""
    secret = "JBSWY3DPEHPK3PXP"
    now = 1800000000
    code = mfa.totp(secret).at(now + offset * 30)
    assert (
        mfa.match_step(secret, code, now=now, last_used_step=None) is not None
    ) is accepted


def test_totp_replay() -> None:
    """protects=used and older steps fail; fails_when=replay passes;
    no_claim=database concurrency is covered separately."""
    secret = "JBSWY3DPEHPK3PXP"
    now = 1800000000
    assert (
        mfa.match_step(
            secret, mfa.totp(secret).at(now), now=now, last_used_step=now // 30
        )
        is None
    )


@pytest.mark.parametrize("code", ["", "12345", "1234567", "１２３４５６"])
def test_malformed_code(code: str) -> None:
    """protects=only six ASCII digits; fails_when=malformed input matches;
    no_claim=none beyond protects."""
    assert (
        mfa.match_step("JBSWY3DPEHPK3PXP", code, now=1800000000, last_used_step=None)
        is None
    )


def test_recovery_code_format_and_hashes() -> None:
    """protects=ten unique hashed recovery codes; fails_when=plaintext stored;
    no_claim=single use is covered against PostgreSQL."""
    from preloop.api.auth.jwt import verify_password

    codes, hashes = mfa.generate_recovery_codes()
    assert len(set(codes)) == len(hashes) == 10
    for code, hashed in zip(codes, hashes, strict=True):
        assert re.fullmatch(r"[a-z2-7]{5}-[a-z2-7]{5}", code)
        assert code not in hashed
        assert verify_password(code, hashed)
        assert not verify_password("wrong-code", hashed)
