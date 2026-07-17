"""Shared helpers for the e2e rig's python modules.

Environment contract (exported by run-oss.sh): RIG_HOST, RIG_URL,
RIG_RELEASE, RIG_CLI_VERSION, RIG_RUN_DIR, RIG_HEADLESS.

Module exit codes: 0 pass, 3 skip (recorded), anything else fail.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SKIP_EXIT = 3

# --- output redaction -------------------------------------------------------
# Error responses can echo request context (Authorization header material,
# submitted payloads — FastAPI validation errors echo the input, including
# passwords). Never log a raw body on failure. Kept in sync with the copy in
# research_agent.py (which is deliberately standalone, stdlib-only).
_REDACT_PATTERNS = [
    # Bearer credentials, wherever they appear.
    re.compile(r"(?i)\bbearer\b[ \t]*[A-Za-z0-9._~+/=-]{4,}"),
    # JWTs (three dot-separated base64url segments starting with eyJ).
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
    # Long opaque token-ish strings.
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
]
_SENSITIVE_JSON_KEY = re.compile(
    r'(?i)("(?:password|token|access_token|refresh_token|secret|api_key|'
    r'authorization)"\s*:\s*")[^"]*(")'
)


def redact(body: Any, limit: int = 400) -> str:
    """Return a failure-safe excerpt of a response body: token-like
    substrings and sensitive JSON values masked, length capped."""
    text = body if isinstance(body, str) else json.dumps(body)
    text = _SENSITIVE_JSON_KEY.sub(r"\1***\2", text)
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("***", text)
    if len(text) > limit:
        text = f"{text[:limit]}... [{len(text) - limit} chars truncated]"
    return text


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing required env var {name}")
    return value


def run_dir() -> Path:
    return Path(env("RIG_RUN_DIR"))


def log(msg: str) -> None:
    print(msg, flush=True)


def note(msg: str) -> None:
    """Record a note into the current step's summary entry."""
    log(f"NOTE: {msg}")
    notes_file = os.environ.get("RIG_STEP_NOTES_FILE")
    if notes_file:
        with open(notes_file, "a") as fh:
            fh.write(msg + "\n")


def skip(msg: str) -> None:
    note(msg)
    sys.exit(SKIP_EXIT)


def load_creds() -> dict[str, Any]:
    return json.loads((run_dir() / "state" / "creds.json").read_text())


def save_creds(creds: dict[str, Any]) -> None:
    path = run_dir() / "state" / "creds.json"
    path.write_text(json.dumps(creds, indent=2))
    path.chmod(0o600)


def load_state(name: str, default: Any = None) -> Any:
    path = run_dir() / "state" / name
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_state(name: str, data: Any) -> None:
    (run_dir() / "state" / name).write_text(json.dumps(data, indent=2))


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS context. Framework/pyenv Pythons often lack a system CA
    bundle for urllib; prefer certifi's bundle when it is importable."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def api_request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: Any = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """HTTPS request with TLS verification ON. Returns (status, parsed body)."""
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        status = exc.code
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return status, parsed


def list_agents(base_url: str, token: str) -> list[dict[str, Any]]:
    """All managed agents for the account (the endpoint pages under 'items')."""
    status, body = api_request(f"{base_url}/api/v1/agents?limit=100", token=token)
    if status != 200:
        raise SystemExit(f"GET /api/v1/agents failed ({status}): {redact(body)}")
    if isinstance(body, dict):
        return body.get("items") or body.get("agents") or []
    return body or []


def user_token(base_url: str, creds: dict[str, Any]) -> str:
    """Mint a fresh user access token via the JSON login endpoint."""
    status, body = api_request(
        f"{base_url}/api/v1/auth/token/json",
        method="POST",
        payload={"username": creds["username"], "password": creds["password"]},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed ({status}): {redact(body)}")
    return body["access_token"]
