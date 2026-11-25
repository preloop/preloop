"""Policy validation, persistence and API boundaries (AC1, 3, 4, 9, 12)."""

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.extension_enforcement_policy import get, upsert
from preloop.models.crud import crud_account
from preloop.schemas.extension_policy import PolicyUpdate
from tests.test_managed_bundle_registry import manifest

URL = "/api/v1/agents/extension-policy/claude_code"
BUNDLES = "/api/v1/agents/managed-config/bundles"


@pytest.mark.parametrize(
    "payload",
    [
        {"options": {"typo": True}},
        {"options": {"managed_hooks_only": "false"}},
        {
            "marketplaces": [
                {"name": "x", "state": "allowed", "source": {"source": "unknown"}}
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "x",
                    "state": "allowed",
                    "source": {"source": "github", "repo": "org/plugins", "typo": "x"},
                }
            ]
        },
        {
            "marketplaces": [
                {"name": "x", "state": s, "source": {"source": "skills-dir"}}
                for s in ["allowed", "blocked"]
            ]
        },
        {
            "marketplaces": [
                {"name": s, "state": s, "source": {"source": "skills-dir"}}
                for s in ["allowed", "blocked"]
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "x",
                    "state": "allowed",
                    "register": True,
                    "source": {"source": "skills-dir"},
                }
            ]
        },
        {
            "marketplaces": [
                {"name": str(i), "state": "allowed", "source": {"source": "skills-dir"}}
                for i in range(51)
            ]
        },
    ],
)
def test_rejects_unsafe_policy(payload: dict[str, Any]) -> None:
    """protects=AC1 strict schemas and contradictory source rejection; fails_when=
    misspelled or incompatible settings are accepted; no_claim=none beyond protects.
    """
    with pytest.raises(ValidationError):
        PolicyUpdate.model_validate(payload)


@pytest.mark.parametrize(
    "source",
    [
        {"source": "github", "repo": "example/plugins"},
        {"source": "git", "url": "https://example.com/plugins.git"},
        {"source": "url", "url": "https://example.com/marketplace.json"},
        {"source": "file", "path": "/opt/plugins/marketplace.json"},
        {"source": "directory", "path": "/opt/plugins"},
        {"source": "hostPattern", "hostPattern": "^example[.]com$"},
        {"source": "pathPattern", "pathPattern": "^/opt/plugins/"},
        {"source": "skills-dir"},
    ],
)
def test_documented_sources(source: dict[str, str]) -> None:
    """protects=eight documented shapes; fails_when=a supported source is rejected;
    no_claim=source existence and regex execution are not tested.
    """
    assert PolicyUpdate(
        marketplaces=[{"name": "example", "state": "allowed", "source": source}]
    )


def test_crud_revision(db_session: Session, test_user: models.User) -> None:
    """protects=AC1 lost update protection and tenant scope; fails_when=stale
    writes win or another account reads policy; no_claim=serial transactions.
    """
    assert (
        get(db_session, account_id=test_user.account_id, agent_kind="claude_code")
        is None
    )
    row = upsert(
        db_session,
        account_id=test_user.account_id,
        agent_kind="claude_code",
        payload=PolicyUpdate(),
        expected_revision=0,
        user_id=test_user.id,
    )
    assert row.revision == 1
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    assert get(db_session, account_id=other.id, agent_kind="claude_code") is None
    with pytest.raises(ValueError, match="revision"):
        upsert(
            db_session,
            account_id=test_user.account_id,
            agent_kind="claude_code",
            payload=PolicyUpdate(),
            expected_revision=0,
            user_id=test_user.id,
        )
    assert (
        upsert(
            db_session,
            account_id=test_user.account_id,
            agent_kind="claude_code",
            payload=PolicyUpdate(),
            expected_revision=1,
            user_id=test_user.id,
        ).revision
        == 2
    )


def test_policy_api(client: TestClient) -> None:
    """protects=AC1 defaults, If-Match, replacement and renderer refusals;
    fails_when=missing or stale revision overwrites; no_claim=fixture identity.
    """
    initial = client.get(URL)
    assert initial.status_code == 200, initial.text
    assert initial.json()["revision"] == 0
    assert not any(initial.json()["options"].values())
    assert client.get(URL + "/managed-settings").status_code == 409
    assert client.put(URL, json={}).status_code == 422
    assert (
        client.put(
            URL, json={"options": {"typo": True}}, headers={"If-Match": "0"}
        ).status_code
        == 422
    )
    saved = client.put(
        URL, json={"options": {"disable_skill_shell": True}}, headers={"If-Match": "0"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert client.put(URL, json={}, headers={"If-Match": "0"}).status_code == 409
    rendered = client.get(URL + "/managed-settings?platform=linux")
    assert rendered.status_code == 200, rendered.text
    assert rendered.json()["platform"] == "linux"
    assert client.get(URL + "/managed-settings?platform=invalid").status_code == 422
    assert client.get(URL.replace("claude_code", "codex")).status_code == 422
    client.put(
        URL,
        json={"options": {"restrict_marketplaces": True}},
        headers={"If-Match": "1"},
    )
    refusal = client.get(URL + "/managed-settings")
    assert refusal.status_code == 422
    assert "official" in refusal.text


def test_hook_and_extension_registration(client: TestClient) -> None:
    """protects=AC4 platform prerequisite, AC9 independent app revisions;
    fails_when=extension registration retires hooks; no_claim=delivery/installation.
    """
    client.put(
        URL, json={"options": {"managed_hooks_only": True}}, headers={"If-Match": "0"}
    )
    assert client.get(URL + "/managed-settings?platform=darwin").status_code == 409
    hook = manifest().model_dump(by_alias=True)
    hook["platform"] = "darwin"
    assert client.post(BUNDLES, json=hook).status_code == 200
    assert client.get(URL + "/managed-settings?platform=darwin").status_code == 200
    assert client.get(URL + "/managed-settings?platform=linux").status_code == 409
    extension = {
        **hook,
        "app_id": "claude-code-extensions",
        "artifact": "50-preloop-extensions.json",
        "keys": ["allowManagedHooksOnly"],
        "delivery": "endpoint_file",
    }
    response = client.post(BUNDLES, json=extension)
    assert response.status_code == 200, response.text
    assert {b["manifest"]["app_id"] for b in client.get(BUNDLES).json()["bundles"]} == {
        "claude-code",
        "claude-code-extensions",
    }


def test_api_permission_checks(client: TestClient) -> None:
    """protects=AC1 administration boundaries in OSS; fails_when=unauthorized rendering
    or writes succeed; no_claim=permission resolver mocked to refuse.
    """
    from fastapi import HTTPException

    with patch(
        "preloop.api.endpoints.extension_policy.ensure_permission_in_oss",
        side_effect=HTTPException(403, "Forbidden"),
    ) as guard:
        assert client.get(URL).status_code == 403
        assert guard.call_args.args[2] == "manage_agents"
        assert client.put(URL, json={}, headers={"If-Match": "0"}).status_code == 403
        assert guard.call_args.args[2] == "manage_agents"
        assert client.get(URL + "/managed-settings").status_code == 403
        assert guard.call_args.args[2] == "manage_agents"


@pytest.mark.parametrize("character", ["\x00", "\x7f", "\x85", "\u202e", "\u200d"])
@pytest.mark.parametrize(
    "source,field",
    [
        ({"source": "url", "url": "https://example.com/catalog.json"}, "url"),
        ({"source": "file", "path": "/opt/plugins/catalog.json"}, "path"),
        ({"source": "directory", "path": "/opt/plugins"}, "path"),
        ({"source": "git", "url": "https://example.com/plugins.git"}, "url"),
        ({"source": "github", "repo": "example/plugins", "ref": "main"}, "ref"),
        ({"source": "hostPattern", "hostPattern": "example[.]com"}, "hostPattern"),
        ({"source": "pathPattern", "pathPattern": "^/opt/plugins"}, "pathPattern"),
    ],
)
def test_source_text_matches_cli_character_safety(
    source: dict[str, str], field: str, character: str
) -> None:
    """protects=API/CLI compatibility; fails_when=stored controls cannot export;
    no_claim=source existence or regular-expression semantics.
    """
    source = {**source, field: source[field] + character}
    with pytest.raises(ValidationError):
        PolicyUpdate(
            marketplaces=[{"name": "example", "state": "allowed", "source": source}]
        )


def test_viewer_cannot_read_marketplace_headers(client: TestClient) -> None:
    """protects=marketplace header confidentiality; fails_when=view-only users
    receive policy credentials; no_claim=permission resolver is a role fixture.
    """
    from fastapi import HTTPException

    saved = client.put(
        URL,
        headers={"If-Match": "0"},
        json={
            "marketplaces": [
                {
                    "name": "example",
                    "state": "allowed",
                    "source": {
                        "source": "url",
                        "url": "https://example.com/catalog.json",
                        "headers": {"Authorization": "Bearer synthetic-test-value"},
                    },
                }
            ]
        },
    )
    assert saved.status_code == 200

    def viewer_only(db: Session, user: models.User, permission: str) -> None:
        """Model a read-only role without administration permissions."""
        if permission != "view_agents":
            raise HTTPException(403, "Forbidden")

    with patch(
        "preloop.api.endpoints.extension_policy.ensure_permission_in_oss",
        side_effect=viewer_only,
    ):
        response = client.get(URL)
    assert response.status_code == 403
    assert "synthetic-test-value" not in response.text


def test_api_policy_round_trips_through_shared_cli_fixture() -> None:
    """protects=API/render/export shape agreement across all source kinds;
    fails_when=accepted source output cannot export; no_claim=vendor delivery.
    """
    import json
    from pathlib import Path

    from preloop.schemas.extension_policy import EnforcementPolicy
    from preloop.services.extension_policy_render import render_overlay

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "cli/internal/cmd/testdata/extension-overlay/sources-roundtrip.json"
        ).read_text()
    )
    policy = EnforcementPolicy.model_validate(fixture["policy"])
    response = render_overlay(policy, [], platform="linux")
    assert response.model_dump(mode="json") == fixture["response"]
