"""Independent PostgreSQL transactions race to consume the same MFA proof."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.api.auth.mfa_router import router
from preloop.models.crud import mfa as crud_mfa
from preloop.models.db.session import get_db_session
from preloop.services import mfa
from preloop.services.authentication_policy import new_authentication
from preloop.utils.encryption import encrypt_value
from tests.test_passkey_only import _user


@pytest.mark.parametrize(
    "same_challenge,recovery",
    [(True, False), (False, False), (True, True), (False, True)],
)
def test_only_one_concurrent_exchange(
    db_engine: Engine, same_challenge: bool, recovery: bool
) -> None:
    """protects=exactly one token pair per raced TOTP or recovery proof;
    fails_when=two independent transactions accept the same proof;
    no_claim=two threads model replicas, with real PostgreSQL row locks."""
    with Session(db_engine) as db:
        user = _user(db)
        obj = crud_mfa.enroll(db, user.id, encrypt_value("JBSWY3DPEHPK3PXP"))
        crud_mfa.confirm(db, obj, now=datetime.now(UTC))
        codes, hashes = mfa.generate_recovery_codes()
        crud_mfa.replace_recovery_codes(db, user.id, hashes)
        db.commit()
        challenges = []
        for _ in range(2):
            with pytest.raises(HTTPException) as required:
                new_authentication(db, user, method="password")
            challenges.append(required.value.detail["challenge_token"])
        if same_challenge:
            challenges[1] = challenges[0]
        proof = (
            {"recovery_code": codes[0]}
            if recovery
            else {"code": mfa.totp("JBSWY3DPEHPK3PXP").now()}
        )
    app = FastAPI()
    app.include_router(router, prefix="/mfa")

    def sessions() -> Any:
        with Session(db_engine) as db:
            yield db

    app.dependency_overrides[get_db_session] = sessions
    start = Barrier(2)

    def exchange(challenge: str) -> tuple[int, bool]:
        with TestClient(app) as client:
            start.wait(timeout=10)
            result = client.post(
                "/mfa/verify", json={"challenge_token": challenge, **proof}
            )
            return result.status_code, "access_token" in result.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(exchange, challenges))
    assert sorted(results) == [(200, True), (401, False)]
