"""Independent final setup review using production envelopes and local fixtures."""

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.services.event_webhooks.events import build_envelope
from preloop.services.event_webhooks.signing import signature_header
from tests.api.test_ci_principal import ci_resources as make_resources
from tests.api.test_ci_principal import provision

_spec = importlib.util.spec_from_file_location(
    "ci_setup_review_verifier",
    Path(__file__).parents[2] / "scripts/restricted_ci_review.py",
)
assert _spec is not None and _spec.loader is not None
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)
FLOW = "00000000-0000-4000-8000-000000000001"
PROJECT = "00000000-0000-4000-8000-000000000002"
EXECUTION = "00000000-0000-4000-8000-000000000003"
ACCOUNT = "00000000-0000-4000-8000-000000000004"
HEAD = "a" * 40


def result() -> dict[str, Any]:
    return dict(
        id=EXECUTION,
        execution_id=EXECUTION,
        flow_id=FLOW,
        project_id=PROJECT,
        repository_identifier="17",
        provider_pr_id="23",
        pr_number=7,
        head_sha=HEAD,
        status="SUCCEEDED",
        result={"review": "Synthetic review"},
    )


def approved() -> dict[str, Any]:
    repository = {"id": 17, "full_name": "example/repository"}
    return dict(
        id=23,
        state="open",
        labels=[{"name": "ci-approved"}],
        head={"sha": HEAD, "repo": repository},
        base={"repo": repository},
    )


def test_real_signed_callback_envelope_matches_authoritative_result() -> None:
    row = result()
    data = {
        name: row[name] for name in (*example.CORRELATION, "execution_id", "status")
    }
    data["result_ready"] = True
    envelope = build_envelope(
        event_id=uuid4(),
        event_type="flow.execution.finished",
        account_id=ACCOUNT,
        data=data,
    )
    raw = json.dumps(envelope).encode()
    secret = "synthetic-receiver-secret"
    # Preserve the production wire bytes/signature; data alone is never what gets signed.
    assert (
        example.verify_callback(
            raw, signature_header(secret, raw, timestamp=1000), secret, row, now=1000
        )
        == data
    )


def publication_args() -> argparse.Namespace:
    return argparse.Namespace(
        url="https://example.com",
        project=PROJECT,
        flow=FLOW,
        repository="example/repository",
        pr=7,
        head=HEAD,
        execution=None,
        timeout=1,
        callback_body=None,
        publish_review=True,
        review_author="github-actions[bot]",
    )


@pytest.mark.parametrize("receipt_state", ["PENDING", "DISMISSED"])
def test_unpublished_or_dismissed_receipt_never_claims_review_publication(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    receipt_state: str,
) -> None:
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-provider-token")
    row = result()
    marker = f"<!-- preloop-ci:{EXECUTION}:{HEAD} -->"
    receipt = dict(
        id=41,
        body=marker,
        commit_id=HEAD,
        user={"id": 31},
        state=receipt_state,
        submitted_at=None,
    )

    def request(
        base: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        if path == "/users/github-actions%5Bbot%5D":
            return {"id": 31}
        if path.endswith("/pulls/7"):
            return approved()
        if "/reviews?" in path:
            return []
        if path.endswith("/reviews") or path.endswith("/reviews/41"):
            return receipt
        if "/flows/executions?" in path:
            return [row]
        if path.endswith(EXECUTION) or path.endswith(EXECUTION + "/result"):
            return row
        raise AssertionError("Unexpected synthetic request")

    monkeypatch.setattr(example, "request_json", request)
    with pytest.raises(example.VerificationError):
        example.run(publication_args())
    assert not capsys.readouterr().out


def test_changed_head_during_receipt_read_never_reports_current_head_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRELOOP_CI_TOKEN", "ci_synthetic")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-provider-token")
    row = result()
    changed = False
    marker = f"<!-- preloop-ci:{EXECUTION}:{HEAD} -->"
    receipt = dict(
        id=41,
        body=marker,
        commit_id=HEAD,
        user={"id": 31},
        state="COMMENTED",
        submitted_at="2026-01-01T00:00:00Z",
    )

    def request(
        base: str, path: str, token: str, method: str = "GET", body: Any = None
    ) -> Any:
        nonlocal changed
        if path == "/users/github-actions%5Bbot%5D":
            return {"id": 31}
        if path.endswith("/pulls/7"):
            pr = approved()
            if changed:
                pr["head"]["sha"] = "b" * 40
            return pr
        if "/reviews?" in path:
            return []
        if path.endswith("/reviews"):
            return receipt
        if path.endswith("/reviews/41"):
            changed = True
            return receipt
        if "/flows/executions?" in path:
            return [row]
        if path.endswith(EXECUTION) or path.endswith(EXECUTION + "/result"):
            return row
        raise AssertionError("Unexpected synthetic request")

    monkeypatch.setattr(example, "request_json", request)
    with pytest.raises(example.VerificationError):
        example.run(publication_args())
    assert not capsys.readouterr().out


def test_cached_human_account_switch_cannot_disable_old_account_principal(
    db_session: Session,
) -> None:
    register_ci_administrator(None)
    resources = make_resources.__wrapped__(db_session)
    owner = resources[0]
    principal, _, _ = provision(db_session, resources)
    other = crud.crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Other synthetic account",
            "is_active": True,
            "primary_user_id": owner.id,
        },
    )
    connection = db_session.connection()
    with Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as first:
        cached = CRUDBase(models.User).get(first, id=owner.id)
        assert cached.account_id == principal.account_id
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as second:
            human = CRUDBase(models.User).get(second, id=owner.id)
            CRUDBase(models.User).update(
                second, db_obj=human, obj_in={"account_id": other.id}
            )
        assert cached.account_id == principal.account_id
        with pytest.raises(PermissionError):
            crud.crud_ci_principal.change(
                first, actor=cached, principal_id=principal.id, enabled=False
            )
        current = CRUDBase(models.CiPrincipal).get(first, id=principal.id)
        first.refresh(current)
        assert current.is_active is True


@pytest.mark.parametrize(
    "mutation", ["foreign_account", "event_type", "version", "extra", "event_id"]
)
def test_signed_wrong_callback_envelope_is_rejected(mutation: str) -> None:
    row = result()
    data = {
        name: row[name] for name in (*example.CORRELATION, "execution_id", "status")
    }
    data["result_ready"] = True
    envelope = build_envelope(
        event_id=uuid4(),
        event_type="flow.execution.finished",
        account_id=ACCOUNT,
        data=data,
    )
    if mutation == "foreign_account":
        envelope["account_id"] = str(uuid4())
    elif mutation == "event_type":
        envelope["type"] = "approval.created"
    elif mutation == "version":
        envelope["version"] = "2"
    elif mutation == "event_id":
        envelope["id"] = "not-an-event-uuid"
    else:
        envelope["unexpected"] = "untrusted"
    raw = json.dumps(envelope).encode()
    secret = "synthetic-receiver-secret"
    with pytest.raises(example.VerificationError):
        example.verify_callback(
            raw,
            signature_header(secret, raw, timestamp=1000),
            secret,
            row,
            now=1000,
            expected_account_id=ACCOUNT,
        )


def test_signed_cancelled_completion_matches_core_terminal_contract() -> None:
    row = result()
    row["status"] = "CANCELLED"
    data = {
        name: row[name] for name in (*example.CORRELATION, "execution_id", "status")
    }
    data["result_ready"] = True
    envelope = build_envelope(
        event_id=uuid4(),
        event_type="flow.execution.finished",
        account_id=ACCOUNT,
        data=data,
    )
    raw = json.dumps(envelope).encode()
    secret = "synthetic-receiver-secret"
    assert (
        example.verify_callback(
            raw,
            signature_header(secret, raw, timestamp=1000),
            secret,
            row,
            now=1000,
            expected_account_id=ACCOUNT,
        )
        == data
    )
