"""Parent session capture for subagent turns (issue #638).

The harness spike (``docs/guide/subagent-session-identity.md``) found two
harnesses that say, on the wire, that a turn belongs to a subagent one of
their own sessions spawned: OpenCode names the parent outright, Claude Code
marks the subagent's turns with an agent id and keeps sending the parent's
session id. This covers reading both, storing the link on the runtime session,
and every way the answer can legitimately be "no parent".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.services.agent_session_headers import (
    claude_code_session_lineage,
    native_parent_session_id_from_headers,
    native_session_id_from_headers,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

_LITELLM_RESPONSE = {
    "id": "chatcmpl_lineage",
    "created": 1710000000,
    "choices": [
        {
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _create_gateway_model(db_session, account_id) -> Any:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _durable_key(db_session, test_user, principal_type: str) -> Any:
    """A durable credential for one agent family: one id reused for every run."""
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name=f"{principal_type} Durable Credential",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "credential_kind": "managed_agent_durable",
            "runtime_principal": {
                "type": principal_type,
                "id": f"{principal_type}-64fd76044120",
                "name": principal_type,
            },
        },
    )
    return runtime_api_key


def _create_embedding_model(db_session, account_id) -> Any:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Embedding Model",
            "provider_name": "openai",
            "model_identifier": "text-embedding-3-small",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/text-embedding-3-small",
                    "provider_adapter": "preloop",
                }
            },
            "is_default": False,
        },
        account_id=account_id,
    )


_LITELLM_EMBEDDING = {
    "object": "list",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "model": "text-embedding-3-small",
    "usage": {"prompt_tokens": 4, "total_tokens": 4},
}


def _run_turn(
    db_session,
    test_user,
    api_key,
    *,
    client_session_id: Optional[str] = None,
    client_parent_session_id: Optional[str] = None,
) -> None:
    """Run one gateway turn the way an endpoint would construct the service."""
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
        client_session_id=client_session_id,
        client_parent_session_id=client_parent_session_id,
    )
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_RESPONSE,
    ):
        service.create_chat_completion(
            {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]}
        )


def _run_embedding(
    db_session,
    test_user,
    api_key,
    *,
    client_session_id: Optional[str] = None,
    client_parent_session_id: Optional[str] = None,
) -> None:
    """Run one embeddings request the way the OpenAI embeddings endpoint would."""
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
        client_session_id=client_session_id,
        client_parent_session_id=client_parent_session_id,
    )
    with patch(
        "preloop.services.openai_gateway.litellm.embedding",
        return_value=_LITELLM_EMBEDDING,
    ):
        service.create_embedding(
            {"model": "openai/text-embedding-3-small", "input": "hello"}
        )


def _usage_rows(db_session) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def _embedding_usage_rows(db_session) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/embeddings")
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def _session_for(db_session, test_user, row: ApiUsage) -> Any:
    session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(row.runtime_session_id),
    )
    assert session is not None
    return session


# ---------------------------------------------------------------------------
# Header derivation
# ---------------------------------------------------------------------------


def test_opencode_parent_header_is_read_for_an_opencode_credential(
    db_session, test_user
):
    """OpenCode names the spawning session in `x-parent-session-id`."""
    api_key = _durable_key(db_session, test_user, "opencode")

    resolved = native_parent_session_id_from_headers(
        {
            "x-session-id": "ses_f599dfca1ffe933RP1BLey87Mr",
            "x-parent-session-id": "ses_f599e0060ffe4gLnO1q69ZcNFw",
        },
        auth_context=SimpleNamespace(api_key=api_key),
    )

    assert resolved == "ses_f599e0060ffe4gLnO1q69ZcNFw"


def test_parent_header_is_ignored_for_other_principal_types(db_session, test_user):
    """`X-Parent-Session-Id` is as generic a name as `X-Session-Id`.

    Any proxy may stamp it, and a wrong lineage is as permanent as a wrong
    session boundary, so the header is trusted only for the agent the
    credential identifies and fails closed to no lineage at all.
    """
    opencode_key = _durable_key(db_session, test_user, "opencode")
    codex_key = _durable_key(db_session, test_user, "codex")
    gemini_key = _durable_key(db_session, test_user, "gemini_cli")
    headers = {"x-parent-session-id": "ses_parent"}

    assert (
        native_parent_session_id_from_headers(
            headers, auth_context=SimpleNamespace(api_key=opencode_key)
        )
        == "ses_parent"
    )
    assert (
        native_parent_session_id_from_headers(
            headers, auth_context=SimpleNamespace(api_key=codex_key)
        )
        is None
    )
    assert (
        native_parent_session_id_from_headers(
            headers, auth_context=SimpleNamespace(api_key=gemini_key)
        )
        is None
    )
    assert (
        native_parent_session_id_from_headers(
            headers, auth_context=SimpleNamespace(api_key=None)
        )
        is None
    )
    assert (
        native_parent_session_id_from_headers(
            None, auth_context=SimpleNamespace(api_key=opencode_key)
        )
        is None
    )


def test_claude_code_agent_id_splits_the_subagent_from_its_parent():
    """A Claude Code subagent turn carries the parent's session id plus its own."""
    lineage = claude_code_session_lineage(
        "ebd4605d-7099-4c54-bd01-747f7a720e1b", "a1e37403a36fc420c"
    )

    assert lineage.session_id == (
        "ebd4605d-7099-4c54-bd01-747f7a720e1b:a1e37403a36fc420c"
    )
    assert lineage.parent_session_id == "ebd4605d-7099-4c54-bd01-747f7a720e1b"


def test_claude_code_parent_turn_keeps_its_session_id_and_has_no_parent():
    """Parent turns send no agent id; their key must not change."""
    lineage = claude_code_session_lineage("ebd4605d-7099-4c54-bd01-747f7a720e1b", None)

    assert lineage.session_id == "ebd4605d-7099-4c54-bd01-747f7a720e1b"
    assert lineage.parent_session_id is None


def test_hostile_claude_code_agent_id_costs_the_lineage_not_the_session():
    """A bad agent id must not take the run's session identity down with it."""
    for hostile in ("../../etc/passwd", "a" * 201, "   ", "agent id"):
        lineage = claude_code_session_lineage(
            "ebd4605d-7099-4c54-bd01-747f7a720e1b", hostile
        )
        assert lineage.session_id == "ebd4605d-7099-4c54-bd01-747f7a720e1b"
        assert lineage.parent_session_id is None


def test_session_id_derivation_is_unchanged_by_the_parent_read(db_session, test_user):
    """Reading a parent must not move the session id itself."""
    opencode_key = _durable_key(db_session, test_user, "opencode")
    codex_key = _durable_key(db_session, test_user, "codex")

    assert (
        native_session_id_from_headers(
            {"x-session-id": "ses_child", "x-parent-session-id": "ses_parent"},
            auth_context=SimpleNamespace(api_key=opencode_key),
        )
        == "ses_child"
    )
    assert (
        native_session_id_from_headers(
            {"session-id": "26d2f152-2d10", "x-parent-session-id": "ses_parent"},
            auth_context=SimpleNamespace(api_key=codex_key),
        )
        == "26d2f152-2d10"
    )


# ---------------------------------------------------------------------------
# Capture, one test per harness the spike found capable
# ---------------------------------------------------------------------------


def test_embedding_first_opencode_subagent_records_its_parent(db_session, test_user):
    """A subagent whose first request is an embedding still records its parent.

    Later chat on the same session must not leave that parent NULL.
    """
    _create_gateway_model(db_session, test_user.account_id)
    _create_embedding_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_embedding(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
        client_parent_session_id="ses_parent",
    )
    child = _session_for(db_session, test_user, _embedding_usage_rows(db_session)[0])
    assert child.parent_session_id is not None

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
        client_parent_session_id="ses_parent",
    )
    db_session.refresh(child)
    chat_row = _usage_rows(db_session)[0]
    assert str(chat_row.runtime_session_id) == str(child.id)
    assert child.parent_session_id is not None
    parent = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(child.parent_session_id),
    )
    assert parent is not None
    assert parent.session_source_id.endswith(":ses_parent")


def test_later_chat_turn_backfills_a_null_parent(db_session, test_user):
    """An already-open child with no parent is filled on the next parented turn."""
    _create_gateway_model(db_session, test_user.account_id)
    _create_embedding_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_embedding(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
    )
    child = _session_for(db_session, test_user, _embedding_usage_rows(db_session)[0])
    assert child.parent_session_id is None

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
        client_parent_session_id="ses_parent",
    )
    db_session.refresh(child)
    assert child.parent_session_id is not None
    parent = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(child.parent_session_id),
    )
    assert parent is not None
    assert parent.session_source_id.endswith(":ses_parent")


def test_opencode_subagent_session_records_its_parent(db_session, test_user):
    """The subagent's own session row points at the session that spawned it."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")
    auth_context = SimpleNamespace(api_key=api_key)
    parent_headers = {"x-session-id": "ses_f599e0060ffe4gLnO1q69ZcNFw"}
    child_headers = {
        "x-session-id": "ses_f599dfca1ffe933RP1BLey87Mr",
        "x-parent-session-id": "ses_f599e0060ffe4gLnO1q69ZcNFw",
    }

    for headers in (parent_headers, child_headers):
        _run_turn(
            db_session,
            test_user,
            api_key,
            client_session_id=native_session_id_from_headers(
                headers, auth_context=auth_context
            ),
            client_parent_session_id=native_parent_session_id_from_headers(
                headers, auth_context=auth_context
            ),
        )

    parent_row, child_row = _usage_rows(db_session)
    parent = _session_for(db_session, test_user, parent_row)
    child = _session_for(db_session, test_user, child_row)
    assert parent.id != child.id
    assert parent.parent_session_id is None
    assert child.parent_session_id == parent.id


def test_claude_code_subagent_session_records_its_parent(db_session, test_user):
    """Claude Code's agent id gives the subagent a row of its own, under the parent."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "claude_code")
    session_uuid = "ebd4605d-7099-4c54-bd01-747f7a720e1b"

    for agent_id in (None, "a1e37403a36fc420c"):
        lineage = claude_code_session_lineage(session_uuid, agent_id)
        _run_turn(
            db_session,
            test_user,
            api_key,
            client_session_id=lineage.session_id,
            client_parent_session_id=lineage.parent_session_id,
        )

    parent_row, child_row = _usage_rows(db_session)
    parent = _session_for(db_session, test_user, parent_row)
    child = _session_for(db_session, test_user, child_row)
    assert parent.id != child.id
    assert parent.session_source_id.endswith(f":{session_uuid}")
    assert child.session_source_id.endswith(f":{session_uuid}:a1e37403a36fc420c")
    assert parent.parent_session_id is None
    assert child.parent_session_id == parent.id


def test_subagent_turn_before_its_parents_next_turn_shares_one_parent_row(
    db_session, test_user
):
    """A subagent can reach the gateway first; the parent must not get two rows."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
        client_parent_session_id="ses_parent",
    )
    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_parent",
    )

    child_row, parent_row = _usage_rows(db_session)
    child = _session_for(db_session, test_user, child_row)
    parent = _session_for(db_session, test_user, parent_row)
    assert child.parent_session_id == parent.id
    assert (
        db_session.query(ApiUsage)
        .filter(ApiUsage.runtime_session_id == str(parent.id))
        .count()
        == 1
    )


# ---------------------------------------------------------------------------
# Every way the answer is legitimately "no parent"
# ---------------------------------------------------------------------------


def test_hostile_or_oversized_parent_value_leaves_a_null_parent(db_session, test_user):
    """The parent uses the session id's own validation and degrades to null."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    for index, hostile in enumerate(
        ("../../etc/passwd", "a" * 201, "ses parent", "\x00ses_parent")
    ):
        _run_turn(
            db_session,
            test_user,
            api_key,
            client_session_id=f"ses_child_{index}",
            client_parent_session_id=hostile,
        )

    rows = _usage_rows(db_session)
    assert len(rows) == 4
    for index, row in enumerate(rows):
        session = _session_for(db_session, test_user, row)
        assert session.parent_session_id is None
        # The session itself is still created and still keyed per run: a
        # rejected parent costs the lineage, never the run's own identity.
        assert session.session_source_id.endswith(f":ses_child_{index}")
    assert len({row.runtime_session_id for row in rows}) == 4


def test_a_session_is_never_its_own_parent(db_session, test_user):
    """A harness echoing the child's own id as the parent must not self-link."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_only",
        client_parent_session_id="ses_only",
    )

    session = _session_for(db_session, test_user, _usage_rows(db_session)[0])
    assert session.parent_session_id is None


def test_incapable_harness_produces_a_null_parent_without_warning_noise(
    db_session, test_user, caplog
):
    """Gemini CLI says nothing about lineage; that is silence, not a problem."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "gemini_cli")
    auth_context = SimpleNamespace(api_key=api_key)
    # Even if something upstream stamps the header, it is not this agent's.
    headers = {"x-parent-session-id": "ses_parent"}

    with caplog.at_level(logging.WARNING):
        _run_turn(
            db_session,
            test_user,
            api_key,
            client_session_id=native_session_id_from_headers(
                headers, auth_context=auth_context
            ),
            client_parent_session_id=native_parent_session_id_from_headers(
                headers, auth_context=auth_context
            ),
        )

    session = _session_for(db_session, test_user, _usage_rows(db_session)[0])
    assert session.parent_session_id is None
    assert not [
        record for record in caplog.records if "parent" in record.getMessage().lower()
    ]


def test_explicit_preloop_session_id_is_never_given_a_derived_parent(
    db_session, test_user
):
    """Our own header wins the key, so a harness-derived parent does not apply."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="run-42",
        client_parent_session_id=None,
    )

    session = _session_for(db_session, test_user, _usage_rows(db_session)[0])
    assert session.session_source_id.endswith(":run-42")
    assert session.parent_session_id is None


def test_a_recorded_parent_is_never_rewritten(db_session, test_user):
    """Lineage is write-once: a later claim cannot move a session's parent."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    _run_turn(
        db_session,
        test_user,
        api_key,
        client_session_id="ses_child",
        client_parent_session_id="ses_first_parent",
    )
    first = _session_for(db_session, test_user, _usage_rows(db_session)[0])
    recorded_parent = first.parent_session_id
    assert recorded_parent is not None

    crud_runtime_session.upsert_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type=first.session_source_type,
        session_source_id=first.session_source_id,
        parent_session_id=first.id,
    )

    db_session.refresh(first)
    assert first.parent_session_id == recorded_parent


def test_sessions_without_lineage_read_back_null(db_session, test_user):
    """The column is nullable and null is the default for every existing row."""
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="custom",
        session_source_id="no-lineage",
        started_at=datetime.now(timezone.utc),
    )

    assert session.parent_session_id is None
