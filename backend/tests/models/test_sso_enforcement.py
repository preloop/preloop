"""Verified domain matching uses the same IDNA form for selective revocation."""

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_person, sso_enforcement


@pytest.mark.parametrize(
    "email",
    ["member@bücher.example", "member@XN--BCHER-KVA.EXAMPLE", "member@bücher.example."],
)
def test_selective_revocation_matches_unicode_domain(
    db_session: Session, test_user: models.User, email: str
) -> None:
    crud_person.update_profile(
        db_session, membership_id=test_user.id, changes={"email": email}
    )
    generation = test_user.auth_generation
    assert (
        sso_enforcement.revoke_non_sso(
            db_session,
            account_id=test_user.account_id,
            domains={"xn--bcher-kva.example"},
        )
        == 1
    )
    assert test_user.non_sso_auth_generation == 1
    assert test_user.auth_generation == generation
