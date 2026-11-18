"""A valid API key on an endpoint that needs a person's console session.

The key is valid, so "Invalid authentication credentials" would mislead the
caller. All identities and key values are synthetic.
"""

import secrets

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_key
from preloop.services.authentication_policy import INTERACTIVE_SESSION_MESSAGE
from tests.test_passkey_only import _user

from tests import test_passkey_only as passkey_support

api = passkey_support.api


def test_api_key_on_session_only_endpoint_is_403_with_stable_code(
    api: TestClient, db_session: Session
) -> None:
    """protects=a valid key is told the action needs a console session;
    fails_when=401 invalid credentials, or the key value is echoed."""
    user = _user(db_session)
    key = "synthetic-" + secrets.token_urlsafe(16)
    crud_api_key.create_with_owner(
        db_session,
        obj_in={"name": "probe"},
        owner_username=user.username,
        key_value=key,
    )
    headers = {"Authorization": f"Bearer {key}"}
    assert api.get("/api/v1/auth/users/me", headers=headers).status_code == 200
    for method, path in (
        ("get", "/api/v1/auth/mfa/status"),
        ("post", "/api/v1/auth/mfa/step-up"),
    ):
        kwargs = {"json": {"code": "000000"}} if method == "post" else {}
        response = getattr(api, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"] == {
            "code": "interactive_session_required",
            "message": INTERACTIVE_SESSION_MESSAGE,
        }
        assert key not in response.text
