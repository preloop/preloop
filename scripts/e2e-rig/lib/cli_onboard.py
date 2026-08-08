"""Shared CLI-onboarding logic for the e2e rig and its CI twin.

Module 08 (`modules/08-cli-onboard.py`) drives the CLI through a real pty on a
VM and records the session; `ci_cli_onboard.py` drives the same CLI headlessly
inside a GitLab job. The transports differ, but the *semantics* must not: how a
login is persisted, how `agents list --json` is parsed, and what counts as a
successfully onboarded agent all live here so the recorded rig and CI can never
drift apart in what they assert.

Stdlib only (the rig's modules run under a bare `python3`).
"""

from __future__ import annotations

import json
from typing import Any

# The CLI's login persistence target and key names; see
# cli/internal/config/config.go (Config struct mapstructure tags).
CLI_CONFIG_RELPATH = ".preloop/config.yaml"


def login_config_yaml(token: str, api_url: str) -> str:
    """Render the CLI's config file for a token login.

    Used instead of `preloop login --token <t>` so the token never lands in
    any argv (visible in `ps`), environment, or asciicast. `preloop login
    --token` writes exactly these three keys via config.SetTokens, so writing
    the file is behaviourally identical and strictly safer.

    json.dumps supplies YAML-compatible double-quoted scalars, which keeps
    tokens containing `:`/`#` from corrupting the document.
    """
    return (
        f"access_token: {json.dumps(token)}\n"
        f'refresh_token: ""\n'
        f"api_url: {json.dumps(api_url.rstrip('/'))}\n"
    )


def agents_from_list_json(stdout: str) -> list[dict[str, Any]]:
    """Parse `preloop agents list --json`.

    The command emits a bare JSON array, but older builds (and the paged
    /api/v1/agents response some tooling forwards) wrap it in an object. Accept
    both rather than making the caller guess which CLI version it is talking
    to. Anything unparseable is an empty list — callers assert on emptiness and
    report the raw text themselves.
    """
    try:
        parsed = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [a for a in parsed if isinstance(a, dict)]
    if isinstance(parsed, dict):
        items = parsed.get("items") or parsed.get("agents") or []
        return [a for a in items if isinstance(a, dict)]
    return []


def onboarded_agent_names(agents: list[dict[str, Any]]) -> list[str]:
    """Display names of agents that actually carry an enrollment."""
    return [
        str(a.get("display_name"))
        for a in agents
        if isinstance(a, dict) and a.get("display_name")
    ]


def find_agents_by_source_type(
    agents: list[dict[str, Any]], source_type: str
) -> list[dict[str, Any]]:
    """Every managed agent enrolled from a given runtime (e.g. claude_code)."""
    wanted = source_type.strip().lower()
    return [
        a
        for a in agents
        if str(a.get("session_source_type", "")).strip().lower() == wanted
    ]


# Onboarding states the backend reports for an agent whose model traffic is
# actually routed through Preloop (see backend account.py enrollment summary).
ROUTED_ONBOARDING_STATES = {"fully_onboarded"}


def is_gateway_routed(agent: dict[str, Any]) -> bool:
    """True when the enrollment claims working model-gateway routing.

    `model_gateway_configured` is the direct signal; `onboarding_state` is
    checked too because a build that has not yet populated the boolean still
    reports the state string.
    """
    if agent.get("model_gateway_configured"):
        return True
    state = str(agent.get("onboarding_state", "")).strip().lower()
    return state in ROUTED_ONBOARDING_STATES


def gateway_env_from_settings(settings: Any) -> tuple[str, str]:
    """Extract (base_url, api_key) that onboarding wrote into an agent config.

    Mirrors applyClaudeManagedGateway in cli/internal/cmd/agents.go, which sets
    env.ANTHROPIC_BASE_URL to "<preloop>/anthropic" and env.ANTHROPIC_API_KEY
    to the minted durable credential. Returns ("", "") when either is absent so
    the caller can fail with its own message.
    """
    if not isinstance(settings, dict):
        return "", ""
    env = settings.get("env")
    if not isinstance(env, dict):
        return "", ""
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    api_key = str(env.get("ANTHROPIC_API_KEY") or "")
    return base_url, api_key
