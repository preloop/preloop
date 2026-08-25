"""Tests for preloop.utils.secret_scrubbing.

The samples below are the shapes reported in issue #173: a tracker
PAT embedded in a git remote URL, surfacing through ``git remote -v`` output
captured into flow execution logs.
"""

from preloop.utils.secret_scrubbing import (
    REDACTED,
    scrub_secret_lines,
    scrub_secrets,
    scrub_structure,
)

# Realistically shaped but fake credentials.
GITHUB_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
GITHUB_CLASSIC = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
GITLAB_PAT = "glpat-aBcDeFgHiJkLmNoPqRs"


class TestIssue173Samples:
    """The exact leak reported in the issue."""

    def test_git_remote_v_fetch_line(self) -> None:
        line = f"origin\thttps://{GITHUB_PAT}@github.com/acme/private.git (fetch)"
        scrubbed = scrub_secrets(line)
        assert GITHUB_PAT not in scrubbed
        assert scrubbed == (
            f"origin\thttps://{REDACTED}@github.com/acme/private.git (fetch)"
        )

    def test_git_remote_v_push_line(self) -> None:
        line = f"origin\thttps://{GITHUB_PAT}@github.com/acme/private.git (push)"
        scrubbed = scrub_secrets(line)
        assert GITHUB_PAT not in scrubbed
        assert scrubbed.endswith("(push)")

    def test_both_remote_lines_at_once(self) -> None:
        lines = [
            f"origin\thttps://{GITHUB_PAT}@github.com/acme/private.git (fetch)",
            f"origin\thttps://{GITHUB_PAT}@github.com/acme/private.git (push)",
        ]
        scrubbed = scrub_secret_lines(lines)
        assert not any(GITHUB_PAT in line for line in scrubbed)
        assert len(scrubbed) == 2

    def test_clone_progress_line(self) -> None:
        line = f"Cloning into 'https://{GITHUB_PAT}@github.com/acme/private.git'..."
        assert GITHUB_PAT not in scrub_secrets(line)

    def test_git_error_line_echoes_the_url(self) -> None:
        """The failure mode behind the errored PR-reviewer pods."""
        line = (
            f"fatal: could not read Username for "
            f"'https://{GITHUB_PAT}@github.com': terminal prompts disabled"
        )
        assert GITHUB_PAT not in scrub_secrets(line)

    def test_config_dump_line(self) -> None:
        line = f"remote.origin.url=https://{GITHUB_PAT}@github.com/acme/private.git"
        assert GITHUB_PAT not in scrub_secrets(line)

    def test_gitlab_variant(self) -> None:
        line = f"origin\thttps://oauth2:{GITLAB_PAT}@gitlab.com/acme/repo.git (fetch)"
        scrubbed = scrub_secrets(line)
        assert GITLAB_PAT not in scrubbed
        assert "oauth2" in scrubbed, "username is not a secret and aids debugging"

    def test_credential_helper_user_is_kept(self) -> None:
        line = f"https://x-access-token:{GITHUB_PAT}@github.com/acme/private.git"
        scrubbed = scrub_secrets(line)
        assert GITHUB_PAT not in scrubbed
        assert (
            scrubbed == f"https://x-access-token:{REDACTED}@github.com/acme/private.git"
        )


class TestBareTokens:
    """Tokens printed outside any URL, e.g. an agent echoing an env var."""

    def test_github_fine_grained_pat(self) -> None:
        scrubbed = scrub_secrets(f"the token is {GITHUB_PAT} ok")
        assert GITHUB_PAT not in scrubbed
        assert "github_pat_" in scrubbed, (
            "prefix is kept so operators know what to rotate"
        )

    def test_github_classic_token(self) -> None:
        assert GITHUB_CLASSIC not in scrub_secrets(f"GITHUB_TOKEN={GITHUB_CLASSIC}")

    def test_gitlab_pat(self) -> None:
        scrubbed = scrub_secrets(f"GITLAB_TOKEN={GITLAB_PAT}")
        assert GITLAB_PAT not in scrubbed
        assert "glpat-" in scrubbed

    def test_anthropic_key(self) -> None:
        key = "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ"
        scrubbed = scrub_secrets(f"ANTHROPIC_API_KEY={key}")
        assert key not in scrubbed
        assert "sk-ant-" in scrubbed

    def test_openai_key(self) -> None:
        key = "sk-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        assert key not in scrub_secrets(f"OPENAI_API_KEY={key}")

    def test_preloop_token(self) -> None:
        token = "preloop_aBcDeFgHiJkLmNoPqRsTuVwXyZ"
        assert token not in scrub_secrets(f"PRELOOP_API_KEY={token}")

    def test_multiple_tokens_in_one_line(self) -> None:
        scrubbed = scrub_secrets(f"{GITHUB_PAT} and also {GITLAB_PAT}")
        assert GITHUB_PAT not in scrubbed
        assert GITLAB_PAT not in scrubbed


class TestAuthHeaders:
    """The PR/MR creation curls are echoed by some agents."""

    def test_authorization_token_header(self) -> None:
        line = f'curl -H "Authorization: token {GITHUB_PAT}" https://api.github.com/x'
        scrubbed = scrub_secrets(line)
        assert GITHUB_PAT not in scrubbed
        assert "Authorization: token [REDACTED]" in scrubbed

    def test_authorization_bearer_header(self) -> None:
        assert GITHUB_PAT not in scrub_secrets(f"Authorization: Bearer {GITHUB_PAT}")

    def test_authorization_basic_header(self) -> None:
        secret = "eC1hY2Nlc3MtdG9rZW46Z2hwX3NlY3JldA=="
        assert secret not in scrub_secrets(f"Authorization: Basic {secret}")

    def test_private_token_header(self) -> None:
        line = f'curl -H "PRIVATE-TOKEN: {GITLAB_PAT}" https://gitlab.com/api/v4/x'
        scrubbed = scrub_secrets(line)
        assert GITLAB_PAT not in scrubbed
        assert "PRIVATE-TOKEN: [REDACTED]" in scrubbed

    def test_gitlab_webhook_token_header(self) -> None:
        assert "s3cr3tvalue" not in scrub_secrets("X-Gitlab-Token: s3cr3tvalue")

    def test_lowercase_header_name(self) -> None:
        assert GITHUB_PAT not in scrub_secrets(f"authorization: bearer {GITHUB_PAT}")


class TestFalsePositives:
    """Scrubbing must not make ordinary logs unreadable."""

    def test_clean_clone_url_is_untouched(self) -> None:
        line = "Cloning into https://github.com/acme/private.git"
        assert scrub_secrets(line) == line

    def test_email_address_is_untouched(self) -> None:
        line = "contact us at support@preloop.ai"
        assert scrub_secrets(line) == line

    def test_git_author_line_is_untouched(self) -> None:
        line = "Author: Jane Doe <jane@example.com>"
        assert scrub_secrets(line) == line

    def test_ssh_remote_is_untouched(self) -> None:
        line = "origin\tgit@github.com:acme/private.git (fetch)"
        assert scrub_secrets(line) == line

    def test_commit_sha_is_untouched(self) -> None:
        line = "26ca5628f4a1b2c3d4e5f60718293a4b5c6d7e8f HEAD"
        assert scrub_secrets(line) == line

    def test_url_with_path_is_untouched(self) -> None:
        line = "GET https://api.github.com/repos/acme/private/pulls 200"
        assert scrub_secrets(line) == line

    def test_short_sk_word_is_untouched(self) -> None:
        """Guard against the sk- pattern eating ordinary hyphenated words."""
        line = "running sk-lint on the tree"
        assert scrub_secrets(line) == line


class TestPassthrough:
    def test_none_passes_through(self) -> None:
        assert scrub_secrets(None) is None

    def test_empty_string_passes_through(self) -> None:
        assert scrub_secrets("") == ""

    def test_scrub_lines_replaces_none_with_empty(self) -> None:
        assert scrub_secret_lines(["", "ok"]) == ["", "ok"]


class TestScrubStructure:
    """Log metadata is JSON, so scrubbing has to reach nested values."""

    def test_nested_dict_values(self) -> None:
        data = {
            "payload": {
                "line": f"origin https://{GITHUB_PAT}@github.com/acme/x.git (fetch)"
            }
        }
        assert GITHUB_PAT not in str(scrub_structure(data))

    def test_list_of_lines(self) -> None:
        data = {"lines": [f"remote: {GITHUB_PAT}", "clean line"]}
        scrubbed = scrub_structure(data)
        assert GITHUB_PAT not in str(scrubbed)
        assert scrubbed["lines"][1] == "clean line"

    def test_keys_are_preserved(self) -> None:
        assert set(scrub_structure({"a": "x", "b": "y"})) == {"a", "b"}

    def test_non_string_scalars_survive(self) -> None:
        data = {"count": 3, "ok": True, "ratio": 1.5, "nothing": None}
        assert scrub_structure(data) == data

    def test_tuples_stay_tuples(self) -> None:
        assert scrub_structure(("a", "b")) == ("a", "b")

    def test_deeply_nested(self) -> None:
        data = {"a": [{"b": {"c": [f"token {GITHUB_PAT}"]}}]}
        assert GITHUB_PAT not in str(scrub_structure(data))


class TestQueryParameterSecrets:
    """Issue #184: API keys passed as URL query parameters were not redacted.

    Gemini takes its key as ``?key=AIza...``; other providers use
    ``?api_key=...`` or OAuth-style ``?access_token=....`` The value must be
    redacted while the parameter name survives, so operators know what to
    rotate.
    """

    GEMINI_KEY = "AIzaSyD-1234567890abcdefghijklmnopqrstu"

    def test_gemini_key_query_param(self) -> None:
        line = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={self.GEMINI_KEY}"
        )
        scrubbed = scrub_secrets(line)
        assert self.GEMINI_KEY not in scrubbed
        assert scrubbed.endswith("models?key=[REDACTED]")
        assert "generativelanguage.googleapis.com" in scrubbed

    def test_api_key_query_param(self) -> None:
        secret = "abcdef1234567890abcdef1234567890"
        line = f"https://api.x.com/v1/models?api_key={secret}"
        scrubbed = scrub_secrets(line)
        assert secret not in scrubbed
        assert "?api_key=[REDACTED]" in scrubbed

    def test_access_token_query_param(self) -> None:
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"
        line = f"https://example.com/oauth/callback?access_token={token}"
        scrubbed = scrub_secrets(line)
        assert token not in scrubbed
        assert "access_token=[REDACTED]" in scrubbed

    def test_param_name_is_preserved_for_rotation(self) -> None:
        line = f"https://host/v1/x?key={self.GEMINI_KEY}&alt=sse"
        scrubbed = scrub_secrets(line)
        assert "key=" in scrubbed
        assert "&alt=sse" in scrubbed

    def test_bare_google_key_format(self) -> None:
        """The AIza format must also be caught outside any URL."""
        assert self.GEMINI_KEY not in scrub_secrets(f"GEMINI_API_KEY={self.GEMINI_KEY}")
        scrubbed = scrub_secrets(f"request failed for key {self.GEMINI_KEY} (403)")
        assert "AIza[REDACTED]" in scrubbed

    def test_second_query_param_after_redacted_one_survives(self) -> None:
        line = "https://host/v1/x?api_key=supersecretvalue123&model=gemini-pro"
        scrubbed = scrub_secrets(line)
        assert "supersecretvalue123" not in scrubbed
        assert "model=gemini-pro" in scrubbed

    def test_non_credential_params_are_untouched(self) -> None:
        line = "GET https://api.github.com/repos/a/b?sort=updated&direction=desc 200"
        assert scrub_secrets(line) == line

    def test_words_ending_in_key_are_not_matched(self) -> None:
        line = "https://host/search?monkey=value&sort_key=abc"
        assert scrub_secrets(line) == line


class TestReDoSResistance:
    """The URL userinfo patterns are applied to every agent log line, so they
    must stay near-linear on adversarial input (CodeQL py/polynomial-redos,
    alerts 189/190). Before the scheme quantifier was bounded, a line of
    80,000 'A' characters took over 5 seconds to scan; bounded, it takes
    milliseconds.
    """

    def test_long_scheme_charset_run_is_fast(self) -> None:
        import time

        adversarial = "A" * 200_000
        start = time.perf_counter()
        assert scrub_secrets(adversarial) == adversarial
        elapsed = time.perf_counter() - start
        # Quadratic behavior at this length is tens of seconds; linear is
        # well under a second even on slow CI machines.
        assert elapsed < 2.0, f"scrub_secrets took {elapsed:.2f}s on 200k chars"

    def test_url_credentials_still_scrubbed_after_bounding(self) -> None:
        line = "https://user:tok_1234567890@github.com/acme/repo.git"
        assert scrub_secrets(line) == (
            f"https://user:{REDACTED}@github.com/acme/repo.git"
        )
