"""The artifact routes are a runner transport, and they must say so.

Round 2 of the CRA dogfood ran ``GET /flows/executions/{id}/artifacts`` with
the same account token that had just read the execution, its logs, its
metrics, its result and its evidence tarball. It answered
``401 invalid_artifact_capability`` and the reviewer recorded it as a
permission defect. It is not one: the route only ever accepts a capability
the orchestrator mints for one execution. The published schema advertised it
anyway, and the reply explained nothing. These tests pin both halves of the
fix.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI, HTTPException

from preloop.api.endpoints import flow_artifacts
from preloop.api.endpoints.flow_artifacts import (
    ARTIFACT_CAPABILITY_ERROR,
    CAPABILITY_AUDIENCE,
    artifact_claims,
    mint_artifact_capability,
)
from preloop.config import settings

ARTIFACT_PATH = "/flows/executions/{execution_id}/artifacts"


def _account_bearer() -> str:
    """A token shaped like the one an operator reads the rest of the run with."""
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "scopes": ["flows:read"],
            "exp": datetime.now(UTC) + timedelta(minutes=30),
        },
        settings.security.secret_key,
        algorithm="HS256",
    )


def _refusal(authorization: str) -> HTTPException:
    with pytest.raises(HTTPException) as excinfo:
        artifact_claims(authorization=authorization)
    assert excinfo.value.status_code == 401
    return excinfo.value


class TestTheRefusalExplainsItself:
    def test_an_account_token_is_told_which_door_this_is(self) -> None:
        detail = _refusal(f"Bearer {_account_bearer()}").detail
        assert isinstance(detail, dict)
        assert detail["error"] == ARTIFACT_CAPABILITY_ERROR
        assert detail["audience"] == CAPABILITY_AUDIENCE
        message = detail["message"]
        assert "capability" in message
        assert "runner" in message.lower()

    def test_the_operator_is_pointed_at_the_endpoint_they_wanted(self) -> None:
        message = _refusal(f"Bearer {_account_bearer()}").detail["message"]
        assert "/api/v1/flows/executions/{execution_id}/evidence" in message
        assert "evidence-status" in message

    def test_the_refusal_is_a_challenge_not_a_bare_401(self) -> None:
        headers = _refusal(f"Bearer {_account_bearer()}").headers or {}
        assert (
            headers.get("WWW-Authenticate") == f'Bearer realm="{CAPABILITY_AUDIENCE}"'
        )

    @pytest.mark.parametrize(
        "authorization",
        ["", "Bearer ", "Token abc", "Bearer not-a-jwt"],
        ids=["absent", "empty", "wrong-scheme", "garbage"],
    )
    def test_every_wrong_credential_gets_the_same_explanation(
        self, authorization: str
    ) -> None:
        assert _refusal(authorization).detail["error"] == ARTIFACT_CAPABILITY_ERROR

    def test_the_reply_names_no_execution_and_no_artifact(self) -> None:
        """It is returned unauthenticated, so it may confirm nothing."""
        execution_id = uuid4()
        detail = _refusal(f"Bearer {_account_bearer()}").detail
        assert str(execution_id) not in str(detail)
        assert "sha256" not in str(detail).lower()

    def test_a_minted_capability_is_still_accepted(self) -> None:
        account_id, flow_id, execution_id = uuid4(), uuid4(), uuid4()
        token = mint_artifact_capability(
            account_id=account_id,
            flow_id=flow_id,
            thread_id="thread-1",
            execution_id=execution_id,
            kind="evidence",
            operation="get",
        )
        claims = artifact_claims(authorization=f"Bearer {token}")
        assert claims["execution_id"] == execution_id
        assert claims["kind"] == "evidence"

    def test_a_capability_for_another_audience_is_not_one(self) -> None:
        token = jwt.encode(
            {
                "aud": "flow-runner",
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "account_id": str(uuid4()),
                "flow_id": str(uuid4()),
                "thread_id": "t",
                "execution_id": str(uuid4()),
                "kind": "evidence",
                "operation": "get",
            },
            settings.security.secret_key,
            algorithm="HS256",
        )
        assert _refusal(f"Bearer {token}").detail["error"] == ARTIFACT_CAPABILITY_ERROR


class TestThePublishedSchema:
    """The OpenAPI document is the operator and SDK contract."""

    @staticmethod
    def _paths() -> dict:
        app = FastAPI()
        app.include_router(flow_artifacts.router, prefix="/api/v1")
        return app.openapi()["paths"]

    def test_the_capability_transport_is_not_advertised(self) -> None:
        assert f"/api/v1{ARTIFACT_PATH}" not in self._paths()

    def test_hiding_it_did_not_unmount_it(self) -> None:
        """Undocumented is not disabled: the runner still gets a 401, not a 404."""
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(flow_artifacts.router, prefix="/api/v1")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/flows/executions/{uuid4()}/artifacts")
        assert response.status_code == 401
        assert response.json()["detail"]["error"] == ARTIFACT_CAPABILITY_ERROR
