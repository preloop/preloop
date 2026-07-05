"""Integration tests for Hermes Agent onboarding.

These tests exercise the runtime-session token flow that the Preloop CLI uses
when onboarding a Hermes Agent installation, and confirm that the durable
credential issued by Preloop can connect to the MCP firewall.

Required env vars (set by GitLab CI):
    PRELOOP_TEST_URL      - base URL of the deployed instance
    PRELOOP_TEST_API_KEY  - valid API key for an account in the deployment

The intent is to mirror what `preloop agents discover` and
`preloop agents onboard hermes` do end-to-end, so we catch regressions in any
of: source-type allowlist, runtime-session creation, managed-agent enrollment,
and gateway authorization for Hermes-issued tokens.
"""

import os
import time
import uuid
from typing import Iterator

import httpx
import pytest

from tests.integration.http_client import create_integration_sync_client
from tests.integration.mcp_client import MCPTestClient


PRELOOP_URL = os.getenv("PRELOOP_TEST_URL", "").rstrip("/")
PRELOOP_API_KEY = os.getenv("PRELOOP_TEST_API_KEY", "")

# Hermes is identified by the canonical session_source_type the Preloop CLI
# emits for `~/.hermes/config.yaml`-based installations.
HERMES_SOURCE_TYPE = "hermes"
_TRANSIENT_GATEWAY_STATUSES = {502, 503, 504}


def _skip_if_missing_env() -> None:
    if not PRELOOP_URL or not PRELOOP_API_KEY:
        pytest.skip("PRELOOP_TEST_URL and PRELOOP_TEST_API_KEY required")


@pytest.fixture(scope="module")
def authed_client() -> Iterator[httpx.Client]:
    """Authenticated HTTP client against the deployed Preloop instance."""
    _skip_if_missing_env()
    headers = {
        "Authorization": f"Bearer {PRELOOP_API_KEY}",
        "Content-Type": "application/json",
    }
    with create_integration_sync_client(
        base_url=PRELOOP_URL,
        headers=headers,
    ) as client:
        yield client


def _mint_hermes_runtime_session_token(
    authed_client: httpx.Client,
    *,
    session_source_id: str,
) -> dict:
    payload = {
        "session_source_type": HERMES_SOURCE_TYPE,
        "session_source_id": session_source_id,
        "session_reference": f"~/.hermes/config.yaml@{session_source_id}",
        "runtime_principal_name": f"Hermes Agent ({session_source_id})",
        "expires_in_minutes": 30,
    }
    last_response: httpx.Response | None = None
    for attempt in range(5):
        response = authed_client.post(
            "/api/v1/auth/runtime-sessions/token",
            json=payload,
        )
        if response.status_code == 201:
            return response.json()
        last_response = response
        if response.status_code in _TRANSIENT_GATEWAY_STATUSES and attempt < 4:
            time.sleep(1.0 * (attempt + 1))
            continue
        break

    assert last_response is not None
    assert last_response.status_code == 201, (
        f"Hermes onboarding failed (HTTP {last_response.status_code}): "
        f"{last_response.text}"
    )
    return last_response.json()


@pytest.mark.integration
def test_hermes_runtime_session_token_can_be_minted(
    authed_client: httpx.Client,
) -> None:
    """`hermes` is accepted as a first-class managed-agent source type."""
    session_id = f"hermes-int-{uuid.uuid4().hex[:8]}"
    body = _mint_hermes_runtime_session_token(
        authed_client, session_source_id=session_id
    )

    assert body["session_source_type"] == HERMES_SOURCE_TYPE
    assert body["session_source_id"] == session_id
    assert body["token"], "Expected a durable credential to be minted for Hermes"


@pytest.mark.integration
def test_hermes_onboarding_creates_managed_agent(authed_client: httpx.Client) -> None:
    """The runtime-session bootstrap must register a Hermes managed agent."""
    session_id = f"hermes-int-{uuid.uuid4().hex[:8]}"
    _mint_hermes_runtime_session_token(authed_client, session_source_id=session_id)

    response = authed_client.get(
        "/api/v1/agents",
        params={"agent_kind": HERMES_SOURCE_TYPE},
    )
    assert response.status_code == 200, (
        f"Listing managed agents failed: {response.status_code} {response.text}"
    )
    payload = response.json()
    agents = payload.get("items", [])
    assert agents, "Expected at least one Hermes managed agent after onboarding"

    matching = [
        agent
        for agent in agents
        if agent.get("session_source_id") == session_id
        and (
            agent.get("session_source_type") == HERMES_SOURCE_TYPE
            or agent.get("agent_kind") == HERMES_SOURCE_TYPE
        )
    ]
    assert matching, (
        f"Expected a Hermes managed agent with session id {session_id}, got {agents}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hermes_durable_token_can_connect_to_mcp_firewall(
    authed_client: httpx.Client,
) -> None:
    """The token Hermes receives from Preloop can reach the MCP firewall."""
    session_id = f"hermes-int-{uuid.uuid4().hex[:8]}"
    body = _mint_hermes_runtime_session_token(
        authed_client, session_source_id=session_id
    )
    durable_token = body["token"]

    async with MCPTestClient(PRELOOP_URL, durable_token) as client:
        assert client.session is not None, (
            "Hermes-issued credential must complete the MCP initialize handshake"
        )
