"""Tests for preloop.utils.redaction module."""

from preloop.utils.redaction import (
    REDACTED_STRING,
    redact_dict,
    redact_for_log,
    SENSITIVE_FIELD_NAMES,
)


class TestRedactDict:
    """Tests for redact_dict function."""

    def test_redacts_password(self) -> None:
        """Password fields are redacted."""
        data = {"username": "alice", "password": "secret123"}
        result = redact_dict(data)
        assert result["username"] == "alice"
        assert result["password"] == REDACTED_STRING

    def test_redacts_api_key(self) -> None:
        """API key fields are redacted."""
        data = {"project": "foo", "api_key": "sk-abc123xyz"}
        result = redact_dict(data)
        assert result["project"] == "foo"
        assert result["api_key"] == REDACTED_STRING

    def test_redacts_token(self) -> None:
        """Token fields are redacted."""
        data = {"user_id": "u1", "access_token": "eyJhbGciOiJIUzI1NiJ9"}
        result = redact_dict(data)
        assert result["user_id"] == "u1"
        assert result["access_token"] == REDACTED_STRING

    def test_redacts_nested_dict(self) -> None:
        """Nested dicts are processed recursively."""
        data = {
            "outer": "ok",
            "credentials": {
                "username": "u",
                "password": "p",
                "nested": {"api_key": "key"},
            },
        }
        result = redact_dict(data)
        assert result["outer"] == "ok"
        assert result["credentials"] == REDACTED_STRING

    def test_redacts_list_of_dicts(self) -> None:
        """Lists of dicts are processed."""
        data = [{"name": "a", "secret": "s1"}, {"name": "b", "token": "t1"}]
        result = redact_dict(data)
        assert result[0]["name"] == "a"
        assert result[0]["secret"] == REDACTED_STRING
        assert result[1]["name"] == "b"
        assert result[1]["token"] == REDACTED_STRING

    def test_preserves_non_sensitive(self) -> None:
        """Non-sensitive fields are preserved."""
        data = {
            "title": "Fix bug",
            "description": "The issue is...",
            "project": "my-org/repo",
            "labels": ["bug", "urgent"],
        }
        result = redact_dict(data)
        assert result == data

    def test_redacts_suffix_patterns(self) -> None:
        """Keys ending with _token, _secret, etc. are redacted."""
        data = {
            "github_api_key": "ghp_xxx",
            "oauth_client_secret": "cs_yyy",
            "webhook_secret": "wh_zzz",
        }
        result = redact_dict(data)
        assert result["github_api_key"] == REDACTED_STRING
        assert result["oauth_client_secret"] == REDACTED_STRING
        assert result["webhook_secret"] == REDACTED_STRING

    def test_returns_copy(self) -> None:
        """Original dict is not mutated."""
        data = {"password": "p"}
        result = redact_dict(data)
        assert data["password"] == "p"
        assert result["password"] == REDACTED_STRING

    def test_scalar_unchanged(self) -> None:
        """Scalar values are returned unchanged."""
        assert redact_dict("hello") == "hello"
        assert redact_dict(42) == 42
        assert redact_dict(None) is None


class TestOmitPreloopMarkers:
    """Trusted markers stay on the stored approval and leave the display copy."""

    def test_drops_top_level_markers_only(self) -> None:
        from preloop.utils.redaction import omit_preloop_markers

        data = {
            "command": "git status",
            "_preloop_source": "cursor",
            "_preloop_repository": {"remote": "github.com/example/repo"},
            "nested": {"_preloop_source": "kept"},
        }
        result = omit_preloop_markers(data)
        assert result == {
            "command": "git status",
            "nested": {"_preloop_source": "kept"},
        }
        assert "_preloop_source" in data

    def test_non_dict_passthrough(self) -> None:
        from preloop.utils.redaction import omit_preloop_markers

        assert omit_preloop_markers("git status") == "git status"
        assert omit_preloop_markers(None) is None


class TestRedactForLog:
    """Tests for redact_for_log function."""

    def test_redacts_and_serializes(self) -> None:
        """Produces JSON string with redacted values."""
        data = {"command": "deploy", "api_key": "sk-xxx"}
        s = redact_for_log(data)
        assert "sk-xxx" not in s
        assert REDACTED_STRING in s
        assert "deploy" in s

    def test_truncates_long_output(self) -> None:
        """Long output is truncated."""
        data = {"x": "a" * 1000}
        s = redact_for_log(data, max_length=100)
        assert len(s) <= 100
        assert "[truncated]" in s

    def test_secrets_never_in_output(self) -> None:
        """Representative secrets never appear in output."""
        secrets = [
            {"password": "super_secret_123"},
            {"api_key": "sk-abc123"},
            {"access_token": "eyJhbGciOiJIUzI1NiJ9.xxx"},
            {"client_secret": "cs_xyz789"},
        ]
        for data in secrets:
            for key, value in data.items():
                s = redact_for_log(data)
                assert value not in s, f"Secret value leaked for key {key}"


class TestSensitiveFieldNames:
    """Tests for SENSITIVE_FIELD_NAMES coverage."""

    def test_common_secret_fields_covered(self) -> None:
        """Common secret-bearing field names are in the set."""
        expected = {
            "password",
            "token",
            "api_key",
            "secret",
            "credential",
            "client_secret",
        }
        assert expected.issubset(SENSITIVE_FIELD_NAMES)
