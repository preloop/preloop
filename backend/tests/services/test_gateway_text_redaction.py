"""Keyword and format rules for gateway and tool-evidence text."""

from __future__ import annotations

import pytest

from preloop.services.model_gateway_events import redact_sensitive_text

_SK = "sk-testsecret1234567890"
# Pieces stay separate so the file does not contain one vendor-shaped literal.
_JWT = ".".join(("eyJhbGciOiJub25lIn0", "eyJzdWIiOiJ4In0", "notasecretSIG"))
_AWS = "AKIAIOSFODNN7EXAMPLE"
_AWS_SESSION = "ASIA" + "IOSFODNN7EXAMPLE"
_SLACK = "xoxb-" + ("0" * 8) + "-" + ("0" * 8) + "-" + ("a" * 16)
_GLPAT = "glpat-" + ("a" * 16)
_GH_PAT = "github_pat_" + ("A" * 22)
_GHP = "ghp_" + ("a" * 10)
_SK_PROJ = "sk-proj-" + ("a" * 20)
_SK_ANT = "sk-ant-api03-" + ("a" * 20)
_PRELOOP = "preloop_" + ("Aa1" * 10) + "Bb"
_RUN = "flow_" + ("Aa1" * 14) + "B"
_CI = "ci_" + ("Aa1" * 14) + "B"
_AGT = "agt_" + ("Aa1" * 14) + "B"
_ENR = "enr_" + ("Aa1" * 14) + "B"
_WHSEC = "whsec_" + ("Aa1" * 14) + "B"
_PEM = "-----BEGIN PRIVATE KEY-----\nnotasecret\n-----END PRIVATE KEY-----"
_REDACTED = "***REDACTED***"

# source, expected text, (rule, count) pairs in category order
_CASES: list[tuple[str, str, list[tuple[str, int]]]] = [
    ("id-token: write", "id-token: write", []),
    ("id-token: read", "id-token: read", []),
    ("id-token: none", "id-token: none", []),
    ("id-token: true", "id-token: true", []),
    ("id-token: false", "id-token: false", []),
    ("id-token: null", "id-token: null", []),
    ('id-token: "write"', 'id-token: "write"', []),
    ("contents: read", "contents: read", []),
    ("token = build_permit(binding, head)", "token = build_permit(binding, head)", []),
    (
        'if token:\n        headers["Authorization"] = "Bearer " + token',
        'if token:\n        headers["Authorization"] = "Bearer " + token',
        [],
    ),
    (
        'auth = b64(("x-access-token:" + gh_token).encode())',
        'auth = b64(("x-access-token:" + gh_token).encode())',
        [],
    ),
    (
        "url = 'https://x-access-token:' + token + '@github.com/o/r.git'",
        "url = 'https://x-access-token:' + token + '@github.com/o/r.git'",
        [],
    ),
    (
        "url = 'https://x-access-token:' + obj.attr + '@github.com/o/r.git'",
        "url = 'https://x-access-token:' + obj.attr + '@github.com/o/r.git'",
        [],
    ),
    (
        "url = 'https://x-access-token:' + self.token + '@github.com/o/r.git'",
        "url = 'https://x-access-token:' + self.token + '@github.com/o/r.git'",
        [],
    ),
    (
        'url = "https://x-access-token:" + token + "@github.com/o/r.git"',
        'url = "https://x-access-token:" + token + "@github.com/o/r.git"',
        [],
    ),
    (
        'f"https://x-access-token:{token}@github.com/Org/repo.git"',
        'f"https://x-access-token:{token}@github.com/Org/repo.git"',
        [],
    ),
    (
        'f"https://x-access-token:{token}@github.com/org/repo.git"',
        'f"https://x-access-token:{token}@github.com/org/repo.git"',
        [],
    ),
    ("token={name}", "token={name}", []),
    ("token={{ name }}", "token={{ name }}", []),
    ("token=%s", "token=%s", []),
    ("token=%(name)s", "token=%(name)s", []),
    ("token=<name>", "token=<name>", []),
    ("password=opensesame", "password=opensesame", []),
    ("PASSWORD = opensesame", "PASSWORD = opensesame", []),
    ("ID_TOKEN=write", "ID_TOKEN=write", []),
    (
        "connect(user=user, password=password)",
        "connect(user=user, password=password)",
        [],
    ),
    ("token=", "token=", []),
    ("token: ", "token: ", []),
    ('token: ""', 'token: ""', []),
    ("token: ''", "token: ''", []),
    ("token = os.environ['TOKEN']", "token = os.environ['TOKEN']", []),
    ('token = os.environ["TOKEN"]', 'token = os.environ["TOKEN"]', []),
    ("token = process.env.TOKEN", "token = process.env.TOKEN", []),
    ("token = user.token", "token = user.token", []),
    ("token = $VAR", "token = $VAR", []),
    ("token = ${VAR}", "token = ${VAR}", []),
    ("token = ${{ secrets.NAME }}", "token = ${{ secrets.NAME }}", []),
    ('token: "${VAR}"', 'token: "${VAR}"', []),
    ('token: "${{ secrets.NAME }}"', 'token: "${{ secrets.NAME }}"', []),
    ("token = gh_token", "token = gh_token", []),
    ("secret = api_key", "secret = api_key", []),
    ("token: str", "token: str", []),
    ("password: SecretStr", "password: SecretStr", []),
    ("token: Optional[str]", "token: Optional[str]", []),
    ("token = sha256", "token = sha256", []),
    ("token: hunter2", "token: hunter2", []),
    ("password: changeme", "password: changeme", []),
    ("max_tokens: 100", "max_tokens: 100", []),
    ("token_count = 3", "token_count = 3", []),
    ("tokenizer = build()", "tokenizer = build()", []),
    ("my-token-name: write", "my-token-name: write", []),
    ("flow_execution_id", "flow_execution_id", []),
    ("preloop_caller_session_id", "preloop_caller_session_id", []),
    ("PASSWORD=opensesame", f"PASSWORD={_REDACTED}", [("keyword_assignment", 1)]),
    (
        "DB_PASSWORD=correcthorsebatterystaple",
        f"DB_PASSWORD={_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        "token=deadbeefcafebabedeadbeefcafebabe",
        f"token={_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        'password = "changeme" + suffix',
        f'password = "{_REDACTED}" + suffix',
        [("keyword_assignment", 1)],
    ),
    (
        'password = "hunter2" + salt',
        f'password = "{_REDACTED}" + salt',
        [("keyword_assignment", 1)],
    ),
    (
        """log.info("don't log password: 'hunter2'")""",
        f"""log.info("don't log password: '{_REDACTED}'")""",
        [("keyword_assignment", 1)],
    ),
    ("password: hunter2", f"password: {_REDACTED}", [("keyword_assignment", 1)]),
    ('password: "hunter2"', f'password: "{_REDACTED}"', [("keyword_assignment", 1)]),
    ("password: 'hunter2'", f"password: '{_REDACTED}'", [("keyword_assignment", 1)]),
    (
        'password: "changeme"',
        f'password: "{_REDACTED}"',
        [("keyword_assignment", 1)],
    ),
    ("secret: hunter2", f"secret: {_REDACTED}", [("keyword_assignment", 1)]),
    ("password = sha256", f"password = {_REDACTED}", [("keyword_assignment", 1)]),
    (
        "client_secret: hunter2",
        f"client_secret: {_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (f"API_KEY={_SK}", f"API_KEY={_REDACTED}", [("keyword_assignment", 1)]),
    (
        'token: "Ab3defghijklmnopQR"',
        f'token: "{_REDACTED}"',
        [("keyword_assignment", 1)],
    ),
    (
        f'"api_key": "{_SK}"',
        f'"api_key": "{_REDACTED}"',
        [("keyword_assignment", 1)],
    ),
    (f"export TOKEN={_SK}", f"export TOKEN={_REDACTED}", [("keyword_assignment", 1)]),
    (
        f"https://example.com/cb?token={_SK}&x=1",
        f"https://example.com/cb?token={_REDACTED}&x=1",
        [("keyword_assignment", 1)],
    ),
    (f"X-Api-Key: {_SK}", f"X-Api-Key: {_REDACTED}", [("keyword_assignment", 1)]),
    (
        "github_token=Ab3defghijklmnop",
        f"github_token={_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        "client_secret = Ab3defghijklmnop",
        f"client_secret = {_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        f"OPENAI_API_KEY={_SK}",
        f"OPENAI_API_KEY={_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        f"id-token: write token={_SK}",
        f"id-token: write token={_REDACTED}",
        [("keyword_assignment", 1)],
    ),
    (
        'password: hunter2\npassword: "changeme"',
        f'password: {_REDACTED}\npassword: "{_REDACTED}"',
        [("keyword_assignment", 2)],
    ),
    (f"see {_JWT} end", f"see {_REDACTED} end", [("jwt", 1)]),
    (f"see {_AWS} end", f"see {_REDACTED} end", [("aws_access_key_id", 1)]),
    (
        f"see {_AWS_SESSION} end",
        f"see {_REDACTED} end",
        [("aws_access_key_id", 1)],
    ),
    (f"see {_SLACK} end", f"see {_REDACTED} end", [("slack_token", 1)]),
    (f"see {_GLPAT} end", f"see {_REDACTED} end", [("gitlab_pat", 1)]),
    (f"see {_GH_PAT} end", f"see {_REDACTED} end", [("github_fine_grained_pat", 1)]),
    (f"see {_GHP} end", f"see {_REDACTED} end", [("github_token", 1)]),
    (f"see {_SK} end", f"see {_REDACTED} end", [("sk_key", 1)]),
    (f"see {_SK_PROJ} end", f"see {_REDACTED} end", [("sk_key", 1)]),
    (f"see {_SK_ANT} end", f"see {_REDACTED} end", [("sk_key", 1)]),
    (f"see {_PRELOOP} end", f"see {_REDACTED} end", [("preloop_key", 1)]),
    (f"see {_CI} end", f"see {_REDACTED} end", [("preloop_key", 1)]),
    (f"see {_AGT} end", f"see {_REDACTED} end", [("preloop_key", 1)]),
    (f"see {_ENR} end", f"see {_REDACTED} end", [("preloop_key", 1)]),
    (f"see {_WHSEC} end", f"see {_REDACTED} end", [("preloop_key", 1)]),
    (f"see {_RUN} end", f"see {_REDACTED} end", [("preloop_run_token", 1)]),
    (f"see {_PEM} end", f"see {_REDACTED} end", [("private_key", 1)]),
    (
        "Authorization: Bearer super-secret-token",
        f"Authorization: Bearer {_REDACTED}",
        [("authorization_bearer", 1)],
    ),
    (f"bearer {'a' * 8}", f"bearer {_REDACTED}", [("bearer_token", 1)]),
    (f"id-token: write {_JWT}", f"id-token: write {_REDACTED}", [("jwt", 1)]),
]


def test_token_fixtures_have_the_issued_lengths() -> None:
    """The fake keys use the same lengths as token_urlsafe(24) and (32)."""
    assert len(_PRELOOP.removeprefix("preloop_")) == 32
    for token, prefix in (
        (_RUN, "flow_"),
        (_CI, "ci_"),
        (_AGT, "agt_"),
        (_ENR, "enr_"),
        (_WHSEC, "whsec_"),
    ):
        assert len(token.removeprefix(prefix)) == 43


@pytest.mark.parametrize(("source", "expected", "rules"), _CASES)
def test_redaction_rules(
    source: str, expected: str, rules: list[tuple[str, int]]
) -> None:
    """Each ordinary shape stays, and each credential shape is masked."""
    text, facts = redact_sensitive_text(source)
    assert text == expected
    assert [(item["rule"], item["count"]) for item in facts] == rules
    for secret in (_SK, _JWT, _AWS, _SLACK, _GLPAT, _GH_PAT, _GHP, _PRELOOP, _RUN):
        if secret in source and secret not in expected:
            assert secret not in text
