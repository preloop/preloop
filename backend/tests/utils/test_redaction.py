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


class TestRedactAgentConfig:
    """Tests for redact_agent_config, used on stored agent enrollment JSON."""

    PROVIDER_KEY = "sk-ant-" + "api03-Example0123456789abcdefghij"
    AGENT_KEY = "agt_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8L9z0X1c"

    def test_redacts_env_style_and_nested_sensitive_keys(self) -> None:
        from preloop.utils.redaction import CONFIG_REDACTED, redact_agent_config

        result = redact_agent_config(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "plain-value",
                    "ANTHROPIC_AUTH_TOKEN": "plain-value",
                    "CLAUDE_CODE_OAUTH_TOKEN": "plain-value",
                    "AWS_SECRET_ACCESS_KEY": "plain-value",
                    "AWS_SESSION_TOKEN": "plain-value",
                    "ANTHROPIC_MODEL": "example-model",
                },
                "experimental_bearer_token": "plain-value",
                "headers": {"Authorization": "Basic abc", "X-Api-Key": "plain"},
                "token": "plain-value",
                "apiKey": "plain-value",
                "secrets": {"nested": ["plain-value", 5]},
                "db_password": "plain-value",
            }
        )

        assert "plain" not in str(result)
        assert result["env"]["ANTHROPIC_MODEL"] == "example-model"
        assert result["env"]["AWS_SESSION_TOKEN"] == CONFIG_REDACTED
        assert result["token"] == CONFIG_REDACTED
        assert result["secrets"] == {"nested": [CONFIG_REDACTED, 5]}

    def test_redacts_secret_looking_values_under_any_key(self) -> None:
        from preloop.utils.redaction import CONFIG_REDACTED, redact_agent_config

        result = redact_agent_config(
            {
                "args": ["--header", f"Authorization: Bearer {self.AGENT_KEY}"],
                "note": self.PROVIDER_KEY,
                "other": "sk-" + "A" * 24,
                "inline": "Bearer abcDEF123456",
                "value": self.AGENT_KEY,
            }
        )

        assert self.AGENT_KEY not in str(result)
        assert self.PROVIDER_KEY not in str(result)
        assert result["note"] == CONFIG_REDACTED
        assert result["other"] == CONFIG_REDACTED
        assert result["inline"] == CONFIG_REDACTED
        assert result["value"] == CONFIG_REDACTED
        assert result["args"][0] == "--header"

    def test_keeps_placeholders_flags_and_references(self) -> None:
        from preloop.utils.redaction import redact_agent_config

        data = {
            "manual_apply": {
                "env": {"ANTHROPIC_API_KEY": "<AGENT_KEY>"},
                "headers": {"Authorization": "Bearer <AGENT_KEY>"},
                "hint": {"api_key": "<AGENT_KEY_LAST_20>", "token": "<redacted>"},
                "limitations": ["Uses bearer authentication for the MCP server."],
                "remove": ["env.ANTHROPIC_AUTH_TOKEN"],
            },
            "control_bearer_token_ok": True,
            "gateway_api_key_configured": False,
            "token": None,
            "max_tokens": 4096,
            "bearer_token_env_var": "PRELOOP_AGENT_KEY",
            "credentials_secret_id": "00000000-0000-0000-0000-000000000001",
            "api_key": "",
            "tokenizer": "example",
        }

        assert redact_agent_config(data) == data

    def test_is_idempotent_and_does_not_mutate(self) -> None:
        from preloop.utils.redaction import redact_agent_config

        data = {"env": {"ANTHROPIC_API_KEY": self.PROVIDER_KEY}}
        once = redact_agent_config(data)

        assert redact_agent_config(once) == once
        assert data["env"]["ANTHROPIC_API_KEY"] == self.PROVIDER_KEY


def test_agent_config_redacts_generic_token_and_key_names() -> None:
    """Rows stored before the CLI sanitiser was fixed hold these in clear."""
    from preloop.utils.redaction import redact_agent_config

    stored = {
        "env": {
            "GITHUB_TOKEN": "ghp_" + "a1" * 18,
            "NPM_TOKEN": "synthetic-npm-value",
            "HF_TOKEN": "synthetic-hf-value",
            "OPENAI_KEY": "synthetic-openai-value",
            "UNRELATED": "ghp_" + "b2" * 18,
            "SLACK": "xoxb-synthetic-000000000000",
            "MAX_THINKING_TOKENS": "8000",
            "ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic",
            "bearer_token_env_var": "EXAMPLE_TOKEN",
            "gateway_token_ok": True,
        }
    }

    env = redact_agent_config(stored)["env"]

    for key in ("GITHUB_TOKEN", "NPM_TOKEN", "HF_TOKEN", "OPENAI_KEY", "UNRELATED"):
        assert env[key] == "<redacted>", key
    assert "xoxb-" not in env["SLACK"]
    assert env["MAX_THINKING_TOKENS"] == "8000"
    assert env["ANTHROPIC_BASE_URL"] == "https://preloop.example.com/anthropic"
    assert env["bearer_token_env_var"] == "EXAMPLE_TOKEN"
    assert env["gateway_token_ok"] is True


def test_agent_config_redacts_the_password_of_a_credential_url() -> None:
    from preloop.utils.redaction import redact_agent_config

    stored = {
        "env": {
            "DATABASE_URL": "postgres://jane:synthetic-pass@db.example.com/app",
            "DOCS": "https://example.com/guide",
            "PORT": "host.example.com:5432",
        },
        "args": ["--url", "https://bot:synthetic-pass@git.example.com/repo.git"],
    }

    redacted = redact_agent_config(stored)

    assert redacted["env"]["DATABASE_URL"] == (
        "postgres://jane:<redacted>@db.example.com/app"
    )
    assert redacted["env"]["DOCS"] == "https://example.com/guide"
    assert redacted["env"]["PORT"] == "host.example.com:5432"
    assert "synthetic-pass" not in redacted["args"][1]
