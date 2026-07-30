"""Tests for WebAuthn passkey ceremony endpoints.

Ceremony verification is mocked (authenticator hardware cannot run in CI);
these tests cover option generation, challenge-token integrity, credential
storage/lookup, token issuance, the feature flag, and credential management.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import decode_token, get_current_active_user
from preloop.api.auth.webauthn_router import (
    _issue_challenge_token,
    _read_challenge_token,
    router as webauthn_router,
)
from preloop.models.models.user import User

app = FastAPI()
app.include_router(webauthn_router, prefix="/auth/webauthn")
client = TestClient(app)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.fixture
def mock_user():
    from datetime import UTC, datetime

    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    user.username = "testuser"
    user.email = "testuser@example.com"
    user.full_name = "Test User"
    user.is_active = True
    user.last_login = datetime.now(UTC)  # recent: no inactivity notification
    return user


@pytest.fixture
def db_session_mock():
    from preloop.models.db.session import get_db_session

    db_session = MagicMock(spec=Session)
    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        yield db_session
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
def authed_user(mock_user):
    app.dependency_overrides[get_current_active_user] = lambda: mock_user
    try:
        yield mock_user
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


class TestChallengeToken:
    """The stateless challenge token that carries ceremony state."""

    def test_roundtrip(self):
        token = _issue_challenge_token(b"challenge-bytes", "register", "user-1")
        payload = _read_challenge_token(token, "register")
        assert payload["challenge_bytes"] == b"challenge-bytes"
        assert payload["user_id"] == "user-1"

    def test_purpose_mismatch_rejected(self):
        from fastapi import HTTPException

        token = _issue_challenge_token(b"x", "register")
        with pytest.raises(HTTPException) as exc:
            _read_challenge_token(token, "authenticate")
        assert exc.value.status_code == 400

    def test_garbage_token_rejected(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            _read_challenge_token("not-a-jwt", "register")
        assert exc.value.status_code == 400


class TestFeatureFlag:
    def test_disabled_returns_404(self, db_session_mock):
        with patch.dict("os.environ", {"PASSKEYS_ENABLED": "false"}):
            response = client.post("/auth/webauthn/authenticate/options")
        assert response.status_code == 404

    def test_enabled_by_default(self, db_session_mock):
        with patch.dict("os.environ", {}, clear=False):
            response = client.post("/auth/webauthn/authenticate/options")
        assert response.status_code == 200


class TestRegistrationCeremony:
    def test_options_include_challenge_and_exclusions(
        self, db_session_mock, authed_user
    ):
        existing = MagicMock()
        existing.credential_id = _b64url(b"existing-cred-id")
        with patch(
            "preloop.api.auth.webauthn_router.crud_webauthn_credential"
        ) as mock_crud:
            mock_crud.list_for_user.return_value = [existing]
            response = client.post("/auth/webauthn/register/options")

        assert response.status_code == 200
        data = response.json()
        assert "challenge" in data["options"]
        assert data["challenge_token"]
        assert data["options"]["rp"]["name"] == "Preloop"
        # Discoverable credentials required
        assert (
            data["options"]["authenticatorSelection"]["residentKey"] == "required"
        )
        # Existing credential excluded from re-registration
        assert len(data["options"]["excludeCredentials"]) == 1

    def test_verify_stores_credential(self, db_session_mock, authed_user):
        challenge_token = _issue_challenge_token(
            b"chal", "register", str(authed_user.id)
        )

        verification = MagicMock()
        verification.credential_id = b"new-cred-id"
        verification.credential_public_key = b"public-key-bytes"
        verification.sign_count = 0
        verification.aaguid = "aaguid-value"

        with (
            patch(
                "preloop.api.auth.webauthn_router.verify_registration_response",
                return_value=verification,
            ) as mock_verify,
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
        ):
            mock_crud.get_by_credential_id.return_value = None

            def fake_refresh(obj):
                obj.id = uuid.uuid4()
                obj.created_at = datetime.now(timezone.utc)

            db_session_mock.refresh.side_effect = fake_refresh

            response = client.post(
                "/auth/webauthn/register/verify",
                json={
                    "credential": {
                        "id": "abc",
                        "response": {"transports": ["internal"]},
                    },
                    "challenge_token": challenge_token,
                    "name": "My laptop",
                },
            )

        assert response.status_code == 200
        assert response.json()["name"] == "My laptop"
        mock_verify.assert_called_once()
        # Challenge bytes from the token were passed to verification
        assert (
            mock_verify.call_args.kwargs["expected_challenge"] == b"chal"
        )
        db_session_mock.add.assert_called_once()
        stored = db_session_mock.add.call_args.args[0]
        assert stored.credential_id == _b64url(b"new-cred-id")
        assert stored.user_id == authed_user.id

    def test_verify_rejects_challenge_for_other_user(
        self, db_session_mock, authed_user
    ):
        challenge_token = _issue_challenge_token(
            b"chal", "register", str(uuid.uuid4())
        )
        response = client.post(
            "/auth/webauthn/register/verify",
            json={
                "credential": {"id": "abc", "response": {}},
                "challenge_token": challenge_token,
            },
        )
        assert response.status_code == 400
        assert "different user" in response.json()["detail"]

    def test_verify_rejects_invalid_attestation(self, db_session_mock, authed_user):
        from webauthn.helpers.exceptions import InvalidRegistrationResponse

        challenge_token = _issue_challenge_token(
            b"chal", "register", str(authed_user.id)
        )
        with patch(
            "preloop.api.auth.webauthn_router.verify_registration_response",
            side_effect=InvalidRegistrationResponse("bad attestation"),
        ):
            response = client.post(
                "/auth/webauthn/register/verify",
                json={
                    "credential": {"id": "abc", "response": {}},
                    "challenge_token": challenge_token,
                },
            )
        assert response.status_code == 400
        assert "could not be verified" in response.json()["detail"]


class TestAuthenticationCeremony:
    def test_options_have_challenge(self, db_session_mock):
        response = client.post("/auth/webauthn/authenticate/options")
        assert response.status_code == 200
        data = response.json()
        assert "challenge" in data["options"]
        assert data["challenge_token"]

    def test_verify_issues_tokens(self, db_session_mock, mock_user):
        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")

        cred = MagicMock()
        cred.user_id = mock_user.id
        cred.public_key = _b64url(b"public-key-bytes")
        cred.sign_count = 5

        verification = MagicMock()
        verification.new_sign_count = 6

        with (
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
            patch(
                "preloop.api.auth.webauthn_router.verify_authentication_response",
                return_value=verification,
            ) as mock_verify,
            patch("preloop.api.auth.webauthn_router.crud_user") as mock_crud_user,
        ):
            mock_crud.get_by_credential_id.return_value = cred
            mock_crud_user.get.return_value = mock_user

            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": _b64url(b"cred-id")},
                    "challenge_token": challenge_token,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["token_type"] == "bearer"
        access = decode_token(data["access_token"])
        assert access.sub == str(mock_user.id)
        assert not access.refresh
        refresh = decode_token(data["refresh_token"])
        assert refresh.refresh is True
        assert refresh.session_started_at is not None
        # Sign counter updated for clone detection
        mock_crud.touch.assert_called_once()
        assert mock_crud.touch.call_args.kwargs["sign_count"] == 6
        # Challenge from token passed to verification
        assert (
            mock_verify.call_args.kwargs["expected_challenge"] == b"auth-chal"
        )

    def test_verify_unknown_credential(self, db_session_mock):
        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")
        with patch(
            "preloop.api.auth.webauthn_router.crud_webauthn_credential"
        ) as mock_crud:
            mock_crud.get_by_credential_id.return_value = None
            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": "unknown"},
                    "challenge_token": challenge_token,
                },
            )
        assert response.status_code == 401
        assert "Unknown passkey" in response.json()["detail"]

    def test_verify_rejects_bad_assertion(self, db_session_mock, mock_user):
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse

        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")
        cred = MagicMock()
        cred.user_id = mock_user.id
        cred.public_key = _b64url(b"public-key-bytes")
        cred.sign_count = 5

        with (
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
            patch(
                "preloop.api.auth.webauthn_router.verify_authentication_response",
                side_effect=InvalidAuthenticationResponse("bad signature"),
            ),
        ):
            mock_crud.get_by_credential_id.return_value = cred
            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": _b64url(b"cred-id")},
                    "challenge_token": challenge_token,
                },
            )
        assert response.status_code == 401

    def test_verify_rejects_inactive_user(self, db_session_mock, mock_user):
        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")
        mock_user.is_active = False
        cred = MagicMock()
        cred.user_id = mock_user.id
        cred.public_key = _b64url(b"public-key-bytes")
        cred.sign_count = 5

        verification = MagicMock()
        verification.new_sign_count = 6

        with (
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
            patch(
                "preloop.api.auth.webauthn_router.verify_authentication_response",
                return_value=verification,
            ),
            patch("preloop.api.auth.webauthn_router.crud_user") as mock_crud_user,
        ):
            mock_crud.get_by_credential_id.return_value = cred
            mock_crud_user.get.return_value = mock_user

            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": _b64url(b"cred-id")},
                    "challenge_token": challenge_token,
                },
            )
        assert response.status_code == 401

    def test_stale_challenge_rejected(self, db_session_mock):
        """An expired challenge token is rejected."""
        from jose import jwt as jose_jwt

        from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY

        expired = jose_jwt.encode(
            {
                "chal": _b64url(b"auth-chal"),
                "purpose": "authenticate",
                "user_id": "",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        response = client.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": {"id": "abc", "rawId": "x"},
                "challenge_token": expired,
            },
        )
        assert response.status_code == 400
        assert "Invalid or expired" in response.json()["detail"]


class TestLoginParityAndRateLimit:
    def test_verify_writes_audit_event(self, db_session_mock, mock_user):
        """Passkey sign-ins must be visible in the audit trail (user.login)."""
        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")

        cred = MagicMock()
        cred.id = uuid.uuid4()
        cred.user_id = mock_user.id
        cred.public_key = _b64url(b"public-key-bytes")
        cred.sign_count = 5

        verification = MagicMock()
        verification.new_sign_count = 6

        with (
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
            patch(
                "preloop.api.auth.webauthn_router.verify_authentication_response",
                return_value=verification,
            ),
            patch("preloop.api.auth.webauthn_router.crud_user") as mock_crud_user,
            patch(
                "preloop.api.auth.webauthn_router.crud_audit_log"
            ) as mock_audit,
        ):
            mock_crud.get_by_credential_id.return_value = cred
            mock_crud_user.get.return_value = mock_user

            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": _b64url(b"cred-id")},
                    "challenge_token": challenge_token,
                },
            )

        assert response.status_code == 200
        mock_audit.log_action.assert_called_once()
        kwargs = mock_audit.log_action.call_args.kwargs
        assert kwargs["action"] == "user.login"
        assert kwargs["user_id"] == mock_user.id
        assert kwargs["details"]["method"] == "passkey"

    def test_verify_notifies_after_inactivity(self, db_session_mock, mock_user):
        """Inactivity notification parity with password login."""
        from datetime import UTC, datetime, timedelta as td

        mock_user.last_login = datetime.now(UTC) - td(days=30)
        challenge_token = _issue_challenge_token(b"auth-chal", "authenticate")

        cred = MagicMock()
        cred.id = uuid.uuid4()
        cred.user_id = mock_user.id
        cred.public_key = _b64url(b"public-key-bytes")
        cred.sign_count = 5

        verification = MagicMock()
        verification.new_sign_count = 6

        with (
            patch(
                "preloop.api.auth.webauthn_router.crud_webauthn_credential"
            ) as mock_crud,
            patch(
                "preloop.api.auth.webauthn_router.verify_authentication_response",
                return_value=verification,
            ),
            patch("preloop.api.auth.webauthn_router.crud_user") as mock_crud_user,
            patch(
                "preloop.services.account_setup_service.notify_admins_user_login_after_inactivity"
            ) as mock_notify,
            # TestClient reports source_ip="testclient", which the parity code
            # (matching password login) deliberately skips; use a real-looking IP.
            patch(
                "preloop.api.auth.webauthn_router.get_client_ip",
                return_value="203.0.113.7",
            ),
        ):
            mock_crud.get_by_credential_id.return_value = cred
            mock_crud_user.get.return_value = mock_user

            response = client.post(
                "/auth/webauthn/authenticate/verify",
                json={
                    "credential": {"id": "abc", "rawId": _b64url(b"cred-id")},
                    "challenge_token": challenge_token,
                },
            )

        assert response.status_code == 200
        # Notification runs in a daemon thread; give it a beat.
        import time

        for _ in range(20):
            if mock_notify.called:
                break
            time.sleep(0.05)
        mock_notify.assert_called_once()

    def test_challenge_rate_limit(self, db_session_mock):
        """Unauthenticated challenge endpoint throttles per IP."""
        from preloop.api.auth import webauthn_router

        original = webauthn_router._rate_buckets.copy()
        webauthn_router._rate_buckets.clear()
        try:
            with patch.object(webauthn_router, "_RATE_LIMIT_MAX_CALLS", 3):
                codes = [
                    client.post("/auth/webauthn/authenticate/options").status_code
                    for _ in range(5)
                ]
            assert codes[:3] == [200, 200, 200]
            assert codes[3] == 429
            assert codes[4] == 429
        finally:
            webauthn_router._rate_buckets.clear()
            webauthn_router._rate_buckets.update(original)


class TestCredentialManagement:
    def test_list_credentials(self, db_session_mock, authed_user):
        cred = MagicMock()
        cred.id = uuid.uuid4()
        cred.name = "My laptop"
        cred.created_at = datetime.now(timezone.utc)
        cred.last_used_at = None

        with patch(
            "preloop.api.auth.webauthn_router.crud_webauthn_credential"
        ) as mock_crud:
            mock_crud.list_for_user.return_value = [cred]
            response = client.get("/auth/webauthn/credentials")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "My laptop"

    def test_delete_own_credential(self, db_session_mock, authed_user):
        cred = MagicMock()
        cred.id = uuid.uuid4()
        cred.user_id = authed_user.id
        cred.is_active = True

        with patch(
            "preloop.api.auth.webauthn_router.crud_webauthn_credential"
        ) as mock_crud:
            mock_crud.get.return_value = cred
            response = client.delete(f"/auth/webauthn/credentials/{cred.id}")

        assert response.status_code == 204
        mock_crud.deactivate.assert_called_once()

    def test_delete_other_users_credential_404(self, db_session_mock, authed_user):
        cred = MagicMock()
        cred.id = uuid.uuid4()
        cred.user_id = uuid.uuid4()  # someone else
        cred.is_active = True

        with patch(
            "preloop.api.auth.webauthn_router.crud_webauthn_credential"
        ) as mock_crud:
            mock_crud.get.return_value = cred
            response = client.delete(f"/auth/webauthn/credentials/{cred.id}")

        assert response.status_code == 404
        mock_crud.deactivate.assert_not_called()
