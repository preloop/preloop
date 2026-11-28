"""Decision metadata for extensions: schema 5 details, capabilities, filters."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_discovered_extension
from preloop.schemas.agent_discovery import DiscoveryReportRequest
from preloop.schemas.discovered_extension import ExtensionMetadata
from preloop.schemas.extension_capabilities import (
    capability_keys,
    extension_capabilities,
)

from .test_extension_inventory import EXTENSIONS, REPORTS, extension, report, store

FIXTURE = Path(__file__).parent / "fixtures" / "extension_inventory"


def v5(extensions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a schema 5 report for the given extensions."""
    payload = report(extensions)
    payload["evidence"]["schema_version"] = 5
    return payload


def labels(kind: str, details: dict[str, Any] | None) -> list[str]:
    """Return capability labels in order."""
    return [cap.label for cap in extension_capabilities(kind, details)]


@pytest.mark.parametrize(
    ("kind", "details", "expected"),
    [
        (
            "skill",
            {
                "tools_declared": True,
                "allowed_tools": ["Bash(git diff:*)", "Read", "Edit", "WebFetch"],
                "hook_events": ["PreToolUse"],
                "script_count": 2,
            },
            [
                "Runs shell commands",
                "Reads files",
                "Writes files",
                "Network access",
                "Registers hooks on PreToolUse",
                "Executes bundled scripts",
            ],
        ),
        ("skill", {"tools_declared": True}, ["Declares no tools"]),
        ("skill", {"description": "No tool list"}, ["Unknown"]),
        ("skill", None, ["Unknown"]),
        (
            "hook",
            {
                "hook_matchers": [
                    {
                        "count": 1,
                        "commands": [
                            {
                                "executable": "x",
                                "preloop_managed": False,
                                "script": True,
                            }
                        ],
                    }
                ]
            },
            ["Runs shell commands", "Executes scripts"],
        ),
        (
            "mcp_server",
            {"transport": "stdio", "command": "npx"},
            ["Runs a local program (npx)", "Adds MCP tools"],
        ),
        (
            "mcp_server",
            {"transport": "http", "url_host": "example.com"},
            ["Network access (example.com)", "Adds MCP tools"],
        ),
        ("mcp_server", {}, ["Unknown"]),
        (
            "plugin",
            {
                "bundles": {
                    "skills": 0,
                    "commands": 2,
                    "hooks": 1,
                    "mcp_servers": 1,
                    "agents": 0,
                },
                "hook_events": ["Stop"],
            },
            [
                "Runs shell commands",
                "Registers hooks on Stop",
                "Adds MCP tools",
                "Some components declare no tools",
            ],
        ),
        (
            "plugin",
            {
                "bundles": {
                    "skills": 0,
                    "commands": 0,
                    "hooks": 0,
                    "mcp_servers": 0,
                    "agents": 0,
                }
            },
            ["Unknown"],
        ),
        ("plugin", None, ["Unknown"]),
    ],
)
def test_capabilities_are_derived_from_collected_facts(
    kind: str, details: dict[str, Any] | None, expected: list[str]
) -> None:
    """protects=plain-words capabilities; fails_when=a capability is guessed
    without a collected fact, or a known fact is not stated."""
    assert labels(kind, details) == expected


def test_realistic_fixture_validates_and_explains_every_row() -> None:
    """protects=the gstack plus official-plugins inventory reaches the API;
    fails_when=a row fails validation, or a skill or plugin has neither a
    purpose nor a capability statement."""
    data = json.loads((FIXTURE / "realistic_inventory.json").read_text())
    rows = [ext for app in data["apps"] for ext in app["extensions"]]
    assert len(rows) > 80
    for row in rows:
        metadata = ExtensionMetadata.model_validate(row)
        details = metadata.details.model_dump() if metadata.details else None
        assert capability_keys(metadata.kind, details)
        if metadata.kind == "skill":
            assert details and details["description"], row["id"]
    withheld = [
        row["id"]
        for row in rows
        if (row.get("details") or {}).get("description_withheld")
    ]
    assert withheld == []


@pytest.mark.parametrize(
    "details",
    [
        {"allowed_tools": ["Read"]},
        {"transport": "stdio"},
        {"homepage_host": "example.com"},
        {"bundle_names": {"skills": ["x"]}},
    ],
)
def test_decision_metadata_requires_schema_5(details: dict[str, Any]) -> None:
    """protects=older replay bytes; fails_when=v4 evidence carries v5 fields."""
    kind = "mcp_server" if "transport" in details else "plugin"
    if "allowed_tools" in details:
        kind = "skill"
    payload = report([extension(kind=kind, details=details)])
    payload["evidence"]["schema_version"] = 4
    with pytest.raises(ValidationError):
        DiscoveryReportRequest.model_validate(payload)
    DiscoveryReportRequest.model_validate(v5([extension(kind=kind, details=details)]))


@pytest.mark.parametrize(
    ("kind", "details"),
    [
        ("skill", {"homepage_host": "https://example.com/x"}),
        ("plugin", {"repository_host": "example.com/team/repo"}),
        ("mcp_server", {"url_host": "10.0.0.1:80"}),
        ("plugin", {"license": "see the file in /opt/x"}),
        ("skill", {"allowed_tools": ["Bash(/opt/x)"]}),
        ("plugin", {"transport": "stdio"}),
        ("hook", {"allowed_tools": ["Bash"]}),
        ("skill", {"modified_at": "yesterday"}),
    ],
)
def test_decision_metadata_rejects_paths_and_wrong_kinds(
    kind: str, details: dict[str, Any]
) -> None:
    """protects=the privacy contract; fails_when=a URL, path or misplaced
    field is accepted."""
    changes: dict[str, Any] = {"kind": kind, "details": details}
    if kind == "hook":
        changes.update(event="Stop", count=1)
    with pytest.raises(ValidationError):
        DiscoveryReportRequest.model_validate(v5([extension(**changes)]))


def test_capability_filter_first_seen_and_people(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=the capability filter and the decision columns; fails_when=a
    filtered list includes a row without the capability, or the aggregate
    lacks first seen and the reporting person."""
    payload = v5(
        [
            extension(
                kind="skill",
                id="guard",
                source_kind="local",
                details={"tools_declared": True, "allowed_tools": ["Bash"]},
            ),
            extension(
                kind="mcp_server",
                id="tracker",
                version=None,
                source_kind="local",
                details={"transport": "http", "url_host": "tracker.example.com"},
            ),
        ]
    )
    assert client.post(REPORTS, json=payload).status_code == 202
    network = client.get(
        EXTENSIONS, params={"group": "extension", "capability": "network"}
    ).json()["items"]
    assert [row["extension_id"] for row in network] == ["tracker"]
    assert network[0]["capabilities"] == [
        {"key": "network", "label": "Network access (tracker.example.com)"},
        {"key": "mcp", "label": "Adds MCP tools"},
    ]
    assert network[0]["first_seen_at"]
    assert network[0]["people_count"] == 1
    shell = client.get(EXTENSIONS, params={"capability": "shell"}).json()["items"]
    assert [row["extension_id"] for row in shell] == ["guard"]
    assert shell[0]["capabilities"][0]["key"] == "shell"
    assert client.get(EXTENSIONS, params={"capability": "danger"}).status_code == 422


def test_capabilities_are_stored_for_filtering(
    db_session: Session, test_user: models.User
) -> None:
    """protects=the stored filter column; fails_when=ingest leaves it empty."""
    store(
        db_session,
        test_user.account_id,
        v5([extension(details={"hook_events": ["Stop"]})]),
    )
    row = db_session.query(models.DiscoveredExtension).one()
    assert row.capabilities == ["shell", "hooks"]
    rows, _ = crud_discovered_extension.list_for_account(
        db_session, account_id=test_user.account_id, capability="hooks"
    )
    assert len(rows) == 1
