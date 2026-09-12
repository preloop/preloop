"""Hosted API selection uses upstream capabilities, never gateway alias guesses."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from preloop.services.model_api_protocol import model_api_protocol
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime


def model(**changes: Any) -> SimpleNamespace:
    values = dict(
        provider_name="openai-compatible",
        api_endpoint="https://opencode.ai/zen/v1",
        model_identifier="responses-fixture",
        meta_data={"gateway": {"enabled": True, "model_alias": "custom-alias"}},
        model_parameters=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "responses"),
        ({"api_endpoint": "https://opencode.ai/zen/v1/"}, "responses"),
        ({"api_endpoint": "https://other.example/v1"}, "chat_completions"),
        ({"model_identifier": "chat-fixture"}, "chat_completions"),
        ({"provider_name": "openrouter"}, "chat_completions"),
        (
            {"meta_data": {"gateway": {"responses_api": "transcode"}}},
            "chat_completions",
        ),
        ({"meta_data": {"responses_api": "transcode"}}, "chat_completions"),
        ({"meta_data": {"responses_api": "typo"}}, "responses"),
        (
            {
                "api_endpoint": "https://other.example/v1",
                "meta_data": {"gateway": {"responses_api": "native"}},
            },
            "responses",
        ),
        (
            {"provider_name": "anthropic", "meta_data": {"responses_api": "native"}},
            "chat_completions",
        ),
    ],
)
def test_protocol_is_scoped_to_upstream(changes: dict[str, Any], expected: str) -> None:
    with patch(
        "preloop.services.model_api_protocol.OPENCODE_ZEN_RESPONSES_MODELS",
        {"responses-fixture"},
    ):
        assert model_api_protocol(model(**changes)) == expected


def test_runtime_preserves_protocol_before_replacing_upstream_with_gateway() -> None:
    with patch(
        "preloop.services.model_api_protocol.OPENCODE_ZEN_RESPONSES_MODELS",
        {"responses-fixture"},
    ):
        runtime = resolve_ai_model_runtime(model())
    context = runtime.to_execution_context(gateway_token="fixture-token")
    assert context["model_identifier"] == "custom-alias"
    assert context["model_api_protocol"] == "responses"
    assert "opencode.ai" not in context["model_endpoint"]


@pytest.mark.parametrize("age_days", [0, 90, 91])
def test_snapshot_age_signal_is_bounded_and_does_not_change_routing(
    age_days: int,
) -> None:
    from datetime import date, timedelta

    from preloop.services import model_api_protocol as catalog

    catalog._warn_stale_snapshot.cache_clear()
    try:
        with (
            patch.object(catalog, "date") as clock,
            patch.object(catalog.logger, "warning") as warning,
            patch.object(
                catalog, "OPENCODE_ZEN_RESPONSES_MODELS", {"responses-fixture"}
            ),
        ):
            clock.today.return_value = date(2030, 1, 1) + timedelta(days=age_days)
            with patch.object(catalog, "OPENCODE_ZEN_SNAPSHOT_DATE", date(2030, 1, 1)):
                assert model_api_protocol(model()) == "responses"
                assert model_api_protocol(model()) == "responses"
                assert (
                    model_api_protocol(model(model_identifier="unknown-fixture"))
                    == "chat_completions"
                )
            assert warning.call_count == (1 if age_days > 90 else 0)
            if warning.called:
                assert catalog.OPENCODE_ZEN_CATALOG_SOURCE in warning.call_args.args
                assert "responses_api" in warning.call_args.args[0]
    finally:
        catalog._warn_stale_snapshot.cache_clear()


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"meta_data": {"gateway": {"responses_api": "native"}}}, "responses"),
        (
            {"meta_data": {"gateway": {"responses_api": "transcode"}}},
            "chat_completions",
        ),
        ({"api_endpoint": "https://mirror.example/zen/v1"}, "chat_completions"),
    ],
)
def test_explicit_protocol_or_unrelated_endpoint_does_not_consult_snapshot(
    changes: dict[str, Any], expected: str
) -> None:
    from preloop.services import model_api_protocol as catalog

    with (
        patch.object(catalog, "_warn_stale_snapshot") as warning,
        patch.object(catalog, "date") as clock,
    ):
        clock.today.side_effect = AssertionError("snapshot must not be consulted")
        assert model_api_protocol(model(**changes)) == expected
    warning.assert_not_called()


def test_running_process_notices_snapshot_aging_without_repeating_warning() -> None:
    from datetime import timedelta

    from preloop.services import model_api_protocol as catalog

    catalog._warn_stale_snapshot.cache_clear()
    try:
        with (
            patch.object(catalog, "date") as clock,
            patch.object(catalog.logger, "warning") as warning,
            patch.object(
                catalog, "OPENCODE_ZEN_RESPONSES_MODELS", {"responses-fixture"}
            ),
        ):
            clock.today.return_value = catalog.OPENCODE_ZEN_SNAPSHOT_DATE
            assert model_api_protocol(model()) == "responses"
            warning.assert_not_called()
            clock.today.return_value += timedelta(
                days=catalog.OPENCODE_ZEN_SNAPSHOT_REVIEW_DAYS + 1
            )
            assert model_api_protocol(model()) == "responses"
            clock.today.return_value += timedelta(days=30)
            assert model_api_protocol(model()) == "responses"
            warning.assert_called_once()
    finally:
        catalog._warn_stale_snapshot.cache_clear()
