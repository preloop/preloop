"""Approval links must land on routes that actually exist (issue #335).

Emitted links used to point at ``/approval/<id>`` while the SPA only served
``/console/approval/:requestId``; the nginx ``^~ /approval/`` block proxies
that prefix to the API, so openers got a bare error page. This pins the
contract from both sides:

* every builder emits a path served by the SPA (or the legacy shim),
* the legacy ``/approval/<id>`` path still redirects to the console page,
* the public token subpaths keep reaching the API in both nginx configs.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

from backend.tests.test_nginx_route_parity import (  # noqa: E402
    _read_docker_template,
    _read_helm_server_block,
    _resolve,
)
from preloop.services.ask_user_inband import approval_console_url
from preloop.services.approval_service import ApprovalService

REPO_ROOT = Path(__file__).resolve().parents[2]
LIT_APP = REPO_ROOT / "frontend" / "src" / "components" / "lit-app.ts"
APP_PY = REPO_ROOT / "backend" / "preloop" / "api" / "app.py"

SAMPLE_ID = uuid.uuid4()


def test_in_band_console_link_matches_spa_route() -> None:
    """The token-free in-band link must be the registered console route."""
    url = approval_console_url("https://app.example.com", SAMPLE_ID)
    assert url == f"https://app.example.com/console/approval/{SAMPLE_ID}"


def test_spa_registers_the_console_approval_route() -> None:
    routes = LIT_APP.read_text(encoding="utf-8")
    assert "path: 'approval/:requestId'" in routes, (
        "frontend/src/components/lit-app.ts no longer registers "
        "/console/approval/:requestId; emitted deep links would 404"
    )


def test_email_and_webhook_builders_emit_console_links() -> None:
    """Tokenized links must target the SPA page, not the bare /approval id."""
    email_source = inspect.getsource(ApprovalService._send_email_notification)
    webhook_source = inspect.getsource(ApprovalService.post_webhook_notification)
    for source in (email_source, webhook_source):
        assert "/console/approval/" in source, (
            "approval link builders must emit /console/approval/<id> "
            "(issue #335); bare /approval/<id> bypasses the SPA route"
        )
        assert '"/approval/' not in source and "'/approval/" not in source, (
            "approval link builders still emit the legacy /approval/<id> path"
        )


def test_escalation_email_emits_console_link() -> None:
    from preloop.utils import email as email_module

    source = inspect.getsource(email_module.send_escalation_email)
    assert "/console/approval/" in source


def test_legacy_approval_path_keeps_redirecting_to_console() -> None:
    """Already-emitted links point at /approval/<id>; they must redirect."""
    source = APP_PY.read_text(encoding="utf-8")
    assert '@app.get("/approval/{request_id}"' in source
    assert "/console/approval/" in source, (
        "the /approval/{request_id} shim must redirect to the console page"
    )


def _assert_proxied(config_name: str, config: str, url: str) -> None:
    body = _resolve(config, url)
    assert body is not None, f"{config_name}: no location block matches {url}"
    assert "proxy_pass" in body, f"{config_name}: {url} is no longer proxied to the API"


def test_public_token_subpaths_reach_the_api_in_both_nginx_configs() -> None:
    """The SPA must not swallow the public token data/decide endpoints."""
    request_id = "3c6f9a52-1111-4c1e-9c66-9d2a5f0f1234"
    for url in (
        f"/approval/{request_id}/data",
        f"/approval/{request_id}/decide",
    ):
        _assert_proxied("docker", _read_docker_template(), url)
        _assert_proxied("helm", _read_helm_server_block(), url)


def test_bare_legacy_link_reaches_the_api_redirect_in_both_configs() -> None:
    """A bare /approval/<id> must hit the backend shim, not the SPA catch-all.

    Either routing works in practice (the SPA page redirects itself), but the
    historical behaviour is the API redirect — pin it so both configs stay in
    sync and old emails keep working.
    """
    request_id = "3c6f9a52-1111-4c1e-9c66-9d2a5f0f1234"
    _assert_proxied("docker", _read_docker_template(), f"/approval/{request_id}")
    _assert_proxied("helm", _read_helm_server_block(), f"/approval/{request_id}")


def test_approval_token_query_survives_link_building() -> None:
    """Console links carry the approval token for signed-out fallback."""
    email_source = inspect.getsource(ApprovalService._send_email_notification)
    assert "token=" in email_source
