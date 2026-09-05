"""Gateway bookkeeping must never be fatal to the customer's request.

Incident 2026-08-05: a JSONB insert of activity metadata failed on a NUL byte.
Because that insert shares the request's SQLAlchemy session, the failed flush
left the session in a pending-rollback state and an upstream SUCCESS became a
customer-visible 502.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


class _Recorder(OpenAIGatewayService):
    """Bare instance exposing only the recording helpers under test."""

    def __init__(self, db):
        super().__init__(
            db,
            ModelGatewayAuthContext(
                token="test",
                user=SimpleNamespace(id="user-1", account_id="account-1"),
            ),
            upstream_backend=MagicMock(),
            skip_runtime_session_resolution=True,
        )


def _service_with_failing_recorder(exc):
    db = MagicMock()
    service = _Recorder(db)

    def _boom(**_kwargs):
        raise exc

    service._record_gateway_request_inner = _boom
    return service, db


def test_recording_failure_does_not_propagate_to_the_request():
    """A failed usage write must not raise into the response path."""
    service, _db = _service_with_failing_recorder(
        RuntimeError("(psycopg2.errors.UntranslatableCharacter)")
    )

    # Must not raise. Before the fix this exception reached the caller and
    # became a 502.
    service._record_gateway_request(
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        ai_model=MagicMock(),
        requested_model="openai/gpt-5",
        response_payload={"usage": {}},
        upstream_response=None,
        endpoint_kind="chat_completions",
    )


def test_recording_failure_rolls_the_session_back():
    """The shared session must be returned to a usable state."""
    service, db = _service_with_failing_recorder(RuntimeError("flush failed"))

    service._record_gateway_request(
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        ai_model=MagicMock(),
        requested_model="openai/gpt-5",
        response_payload={"usage": {}},
        upstream_response=None,
        endpoint_kind="chat_completions",
    )

    # Catching alone leaves the session poisoned for every later query.
    db.rollback.assert_called_once()


def test_recording_failure_logs_the_exception_type_not_the_body(caplog):
    """Log the failure class, never the payload that triggered it."""
    secret_body = "customer-content-that-must-not-be-logged"
    service, _db = _service_with_failing_recorder(RuntimeError(secret_body))

    with caplog.at_level(logging.WARNING):
        service._record_gateway_request(
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            ai_model=MagicMock(),
            requested_model="openai/gpt-5",
            response_payload={"usage": {}},
            upstream_response=None,
            endpoint_kind="chat_completions",
        )

    combined = caplog.text
    assert "RuntimeError" in combined
    assert secret_body not in combined


def test_rollback_helper_survives_a_dead_connection(caplog):
    """A rollback that itself fails must still not raise."""
    db = MagicMock()
    db.rollback.side_effect = RuntimeError("connection already closed")
    service = _Recorder(db)

    with caplog.at_level(logging.WARNING):
        service._rollback_activity_recording(
            ValueError("original failure"), context="unit test"
        )

    assert "ValueError" in caplog.text


@pytest.mark.parametrize("status_code", [200, 201])
def test_successful_calls_are_unaffected_when_recording_works(status_code):
    """The happy path must still record exactly once."""
    db = MagicMock()
    service = _Recorder(db)
    calls = []

    service._record_gateway_request_inner = lambda **kwargs: calls.append(kwargs)

    service._record_gateway_request(
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=status_code,
        duration=0.1,
        ai_model=MagicMock(),
        requested_model="openai/gpt-5",
        response_payload={"usage": {}},
        upstream_response=None,
        endpoint_kind="chat_completions",
    )

    assert len(calls) == 1
    assert calls[0]["status_code"] == status_code
    db.rollback.assert_not_called()


def test_activity_crud_sanitizes_metadata_before_the_jsonb_write():
    """The crud layer is the chokepoint: every caller gets sanitized."""
    from unittest.mock import patch

    from preloop.models.crud.runtime_session_activity import (
        CRUDRuntimeSessionActivity,
    )
    from preloop.models.models.runtime_session_activity import RuntimeSessionActivity

    crud = CRUDRuntimeSessionActivity(RuntimeSessionActivity)
    db = MagicMock()
    # The session lookup that _touch_runtime_session_and_agent performs.
    db.get.return_value = None

    with patch.object(crud, "_touch_runtime_session_and_agent"):
        activity = crud.log_model_gateway_call(
            db,
            account_id="acct-1",
            runtime_session_id="sess-1",
            status="success",
            metadata={"response": {"body": "gzip\x1f\x8b\x08\x00body"}},
            commit=False,
        )

    stored = activity.metadata_["response"]["body"]
    assert stored == "gzipbody"
    assert "\x00" not in stored
