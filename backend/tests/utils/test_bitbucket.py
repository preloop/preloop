"""Tests for the pure Bitbucket Cloud helpers."""

from datetime import datetime, timezone

import pytest

from preloop.utils import bitbucket as bb

# Published GitHub-style test vector; Bitbucket uses the same scheme.
SECRET = "It's a Secret to Everybody"
BODY = b"Hello World!"
SIGNATURE = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"


def test_compute_signature_matches_vector() -> None:
    assert bb.compute_signature(SECRET, BODY) == SIGNATURE


@pytest.mark.parametrize(
    ("secret", "header", "expected"),
    [
        (SECRET, SIGNATURE, True),
        (SECRET, f"  {SIGNATURE} ", True),
        (SECRET, "sha256=deadbeef", False),
        (SECRET, SIGNATURE.replace("sha256=", "sha1="), False),
        (SECRET, None, False),
        (SECRET, "", False),
        ("", SIGNATURE, False),
        ("other", SIGNATURE, False),
    ],
)
def test_verify_signature(secret: str, header: str | None, expected: bool) -> None:
    assert bb.verify_signature(secret, BODY, header) is expected


def test_verify_signature_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = bb.hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(bb.hmac, "compare_digest", spy)
    assert bb.verify_signature(SECRET, BODY, SIGNATURE)
    assert calls == [(SIGNATURE, SIGNATURE)]


def _details(**extra: str) -> dict[str, str]:
    return {"workspace": "ws", **extra}


def test_validate_accepts_api_token() -> None:
    bb.validate_bitbucket_config(
        api_key="tok", auth_type="api_token", connection_details=_details()
    )


def test_validate_accepts_oauth_and_defaults() -> None:
    bb.validate_bitbucket_config(
        api_key="tok", auth_type="oauth_token", connection_details=_details()
    )
    bb.validate_bitbucket_config(
        api_key="tok", auth_type=None, connection_details=_details()
    )


def test_validate_accepts_access_token_with_repository() -> None:
    bb.validate_bitbucket_config(
        api_key="tok",
        auth_type="api_token",
        connection_details=_details(token_kind="access_token", repository="repo"),
    )


@pytest.mark.parametrize(
    ("api_key", "auth_type", "details", "fragment"),
    [
        ("tok", "app_password", _details(), "app passwords are not supported"),
        (
            "tok",
            "api_token",
            _details(token_kind="app_password"),
            "app passwords are not supported",
        ),
        ("ATBBxyz", "api_token", _details(), "app passwords are not supported"),
        ("tok", "basic", _details(), "Unsupported Bitbucket auth_type"),
        ("", "api_token", _details(), "token is required"),
        ("tok", "api_token", {}, "requires 'workspace'"),
        ("tok", "api_token", _details(token_kind="weird"), "token_kind"),
        (
            "tok",
            "api_token",
            _details(token_kind="access_token"),
            "set 'repository'",
        ),
        (
            "tok",
            "api_token",
            _details(username="someone@example.com"),
            "must not be an email",
        ),
    ],
)
def test_validate_rejects(
    api_key: str, auth_type: str, details: dict[str, str], fragment: str
) -> None:
    with pytest.raises(bb.BitbucketConfigError) as exc:
        bb.validate_bitbucket_config(
            api_key=api_key, auth_type=auth_type, connection_details=details
        )
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    ("auth_type", "details", "expected"),
    [
        ("api_token", {"username": "dev-user"}, "dev-user"),
        ("api_token", {}, "x-bitbucket-api-token-auth"),
        (
            "api_token",
            {"username": "dev@example.com", "email": "dev@example.com"},
            "x-bitbucket-api-token-auth",
        ),
        ("api_token", {"token_kind": "access_token"}, "x-token-auth"),
        ("oauth_token", {"username": "dev-user"}, "x-token-auth"),
    ],
)
def test_git_username_never_email(
    auth_type: str, details: dict[str, str], expected: str
) -> None:
    username = bb.git_username_for(auth_type=auth_type, connection_details=details)
    assert username == expected
    assert "@" not in username


def test_parse_pull_request_url() -> None:
    assert bb.parse_pull_request_url(
        "https://bitbucket.org/ws/repo/pull-requests/12/diff#chg"
    ) == ("ws", "repo", 12)
    assert bb.parse_pull_request_url("https://github.com/o/r/pull/1") is None
    assert bb.parse_pull_request_url("") is None
    assert bb.parse_pull_request_path("/ws/repo/pull-requests/3") == ("ws", "repo", 3)
    assert bb.parse_pull_request_path("/ws/repo/src") is None


def test_payload_helpers() -> None:
    pr_payload = {
        "pullrequest": {
            "id": 5,
            "title": "Add parser",
            "description": None,
            "state": "OPEN",
            "author": {"nickname": "dev", "display_name": "Dev"},
            "links": {
                "html": {"href": "https://bitbucket.org/ws/repo/pull-requests/5"}
            },
            "source": {"branch": {"name": "feature"}, "commit": {"hash": "abc"}},
            "destination": {"branch": {"name": "main"}},
        },
        "repository": {"full_name": "ws/repo"},
    }
    assert bb.pr_source_branch(pr_payload) == "feature"
    assert bb.pr_target_branch(pr_payload) == "main"
    assert bb.payload_commit_hash(pr_payload) == "abc"
    assert bb.repository_full_name(pr_payload) == "ws/repo"
    attrs = bb.build_object_attributes(pr_payload["pullrequest"])
    assert attrs["state"] == "open"
    assert attrs["description"] == ""
    assert attrs["author"] == "dev"
    assert attrs["number"] == attrs["iid"] == 5
    assert attrs["url"].endswith("/pull-requests/5")
    assert attrs["last_commit"] == {"id": "abc"}

    push_payload = {
        "push": {
            "changes": [
                {"new": {"name": "old", "target": {"hash": "111"}}},
                {"new": {"name": "topic", "target": {"hash": "222"}}},
                {"new": None},
            ]
        }
    }
    assert bb.payload_push_branch(push_payload) == "topic"
    assert bb.payload_commit_hash(push_payload) == "222"
    assert bb.payload_push_branch({}) is None
    assert bb.normalize_uuid("{abc-1}") == "abc-1"
    assert bb.user_name(None) is None


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("not a date", None),
        ("2026-09-01", "expired"),
        ("2026-10-05T00:00:00Z", "expiring"),
        ("2027-01-31", "ok"),
    ],
)
def test_token_expiry_status(value: str | None, expected: str | None) -> None:
    assert bb.token_expiry_status(value, now=NOW) == expected
