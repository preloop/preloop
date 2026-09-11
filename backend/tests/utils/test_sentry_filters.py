"""Tests for GlitchTip/Sentry benign-event filtering."""

from starlette.websockets import WebSocketDisconnect

from preloop.utils.sentry_filters import sentry_before_send, should_drop_sentry_event


class TestSentryFilters:
    def test_drops_unclosed_aiohttp_session_warning(self) -> None:
        event = {
            "message": "Unclosed client session client_session: <aiohttp...>",
        }
        assert should_drop_sentry_event(event) is True
        assert sentry_before_send(event, {}) is None

    def test_drops_websocket_disconnect(self) -> None:
        event = {"message": "disconnect"}
        hint = {
            "exc_info": (
                WebSocketDisconnect,
                WebSocketDisconnect(1012),
                None,
            )
        }
        assert should_drop_sentry_event(event, hint) is True

    def test_drops_transient_database_disconnect(self) -> None:
        class OperationalError(Exception):
            pass

        event = {"message": "db error"}
        hint = {
            "exc_info": (
                OperationalError,
                OperationalError("SSL connection has been closed unexpectedly"),
                None,
            )
        }
        assert should_drop_sentry_event(event, hint) is True

    def test_keeps_unexpected_runtime_error(self) -> None:
        event = {"message": "Unhandled failure in billing webhook"}
        hint = {"exc_info": (RuntimeError, RuntimeError("boom"), None)}
        assert should_drop_sentry_event(event, hint) is False
        assert sentry_before_send(event, hint) == event


def test_filters_only_automatic_owned_openai_upstream_errors() -> None:
    import httpx
    from openai import InternalServerError

    from preloop.utils.sentry_filters import gateway_upstream_call

    error = InternalServerError(
        "provider unavailable",
        response=httpx.Response(
            500, request=httpx.Request("POST", "https://example.com")
        ),
        body=None,
    )
    event = {
        "exception": {
            "values": [
                {
                    "type": "InternalServerError",
                    "mechanism": {"type": "openai", "handled": False},
                }
            ]
        }
    }
    hint = {"exc_info": (type(error), error, None)}
    assert not should_drop_sentry_event(event, hint)
    with gateway_upstream_call():
        assert should_drop_sentry_event(event, hint)
        assert not should_drop_sentry_event(event, {})
        assert not should_drop_sentry_event(
            event, {"exc_info": (RuntimeError, RuntimeError("adapter bug"), None)}
        )
        assert not should_drop_sentry_event(
            {"exception": {"values": [{"mechanism": {"type": "generic"}}]}}, hint
        )
        with gateway_upstream_call():
            assert should_drop_sentry_event(event, hint)
        assert should_drop_sentry_event(event, hint)
    assert not should_drop_sentry_event(event, hint)


def test_owned_context_resets_on_exception() -> None:
    import pytest

    from preloop.utils.sentry_filters import (
        _GATEWAY_UPSTREAM_CALL,
        gateway_upstream_call,
    )

    with pytest.raises(RuntimeError), gateway_upstream_call():
        raise RuntimeError("local failure")
    assert _GATEWAY_UPSTREAM_CALL.get() is False
