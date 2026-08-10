"""Admin alerting for models the price catalog cannot price.

1667 unpriced gateway calls accumulated in production without anyone being
told. These tests pin the alerting contract:

  1. An unpriceable model notifies admins with the model, account and token
     volume needed to act.
  2. Repeat traffic for the same (model_alias, provider) inside the cooldown
     notifies once, not once per request. The dedup marker is persisted in the
     database so it holds across the multiple API/gateway replicas that run in
     production (a past incident was caused by per-process state).
  3. A notification failure never propagates into the request path.
"""

from unittest.mock import patch

import pytest

from preloop.services import unpriced_model_alert
from preloop.services.unpriced_model_alert import notify_unpriced_model


@pytest.fixture(autouse=True)
def _clear_alert_cache():
    """Isolate the process-local fast-path cache between tests."""
    unpriced_model_alert.reset_alert_state_for_tests()
    yield
    unpriced_model_alert.reset_alert_state_for_tests()


def test_first_unpriced_model_notifies_admins(db_session, test_user):
    """The first sighting alerts with model, provider, account and volume."""
    account_id = str(test_user.account_id)

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        sent = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=62932308,
        )

    assert sent is True
    mock_notify.assert_called_once()
    body = mock_notify.call_args.args[1]
    assert "deepseek/deepseek-v4-flash-0731" in body
    assert "openai-compatible" in body
    assert account_id in body
    assert "62,932,308" in body


def test_repeat_unpriced_model_is_deduped_within_cooldown(db_session, test_user):
    """Hot-path repeats collapse to a single notification per model/provider."""
    account_id = str(test_user.account_id)

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        results = [
            notify_unpriced_model(
                db_session,
                account_id=account_id,
                model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
                provider_name="openai-compatible",
                total_tokens=1200,
            )
            for _ in range(25)
        ]

    assert results.count(True) == 1
    assert mock_notify.call_count == 1


def test_distinct_models_alert_independently(db_session, test_user):
    """Dedup is per (model_alias, provider), not global."""
    account_id = str(test_user.account_id)

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=10,
        )
        notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai/moonshotai/kimi-k3",
            provider_name="openai",
            total_tokens=20,
        )

    assert mock_notify.call_count == 2


def test_dedup_marker_is_persisted_not_process_local(db_session, test_user):
    """The cooldown survives a fresh process: the marker lives in the DB."""
    account_id = str(test_user.account_id)

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=10,
        )
    assert mock_notify.call_count == 1

    # Simulate a different replica / restarted process: no in-memory state.
    unpriced_model_alert.reset_alert_state_for_tests()

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        sent = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=10,
        )

    assert sent is False
    mock_notify.assert_not_called()


def test_notification_failure_never_raises(db_session, test_user):
    """A broken notification channel must not fail the user's request."""
    account_id = str(test_user.account_id)

    with patch.object(
        unpriced_model_alert, "notify_admins", side_effect=RuntimeError("smtp down")
    ):
        sent = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=10,
        )

    assert sent is False


def test_database_failure_never_raises(db_session, test_user):
    """A dedup-store failure degrades quietly instead of breaking the gateway."""
    account_id = str(test_user.account_id)

    with patch.object(
        unpriced_model_alert.crud_audit_log,
        "log_action",
        side_effect=RuntimeError("db down"),
    ):
        sent = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
            provider_name="openai-compatible",
            total_tokens=10,
        )

    assert sent is False


def test_alias_spellings_of_one_model_collapse_to_one_alert(db_session, test_user):
    """Three spellings of one gateway model must not triple-alert.

    In production the OpenRouter Auto Router was reachable as
    ``openrouter/auto-beta``, ``openai-compatible/openrouter/auto-beta`` and
    ``openrouter/openrouter/auto-beta`` (three AIModel configs pointing at the
    same upstream model), and each spelling fired its own admin alert. The
    dedup key must canonicalise via the resolver's alias candidates so all
    spellings share one cooldown.
    """
    from preloop.models.models.ai_model import AIModel

    account_id = str(test_user.account_id)
    spellings = [
        AIModel(
            provider_name="openrouter",
            model_identifier="openrouter/auto-beta",
            api_endpoint="https://openrouter.ai/api/v1",
        ),
        AIModel(
            provider_name="openai-compatible",
            model_identifier="openrouter/auto-beta",
            api_endpoint="https://openrouter.ai/api/v1",
            meta_data={
                "gateway": {"model_alias": "openai-compatible/openrouter/auto-beta"}
            },
        ),
        AIModel(
            provider_name="openrouter",
            model_identifier="openrouter/auto-beta",
            meta_data={"gateway": {"model_alias": "openrouter/openrouter/auto-beta"}},
        ),
    ]
    aliases = [
        "openrouter/auto-beta",
        "openai-compatible/openrouter/auto-beta",
        "openrouter/openrouter/auto-beta",
    ]

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        results = [
            notify_unpriced_model(
                db_session,
                account_id=account_id,
                model_alias=alias,
                provider_name=model.provider_name,
                total_tokens=100,
                ai_model=model,
            )
            for model, alias in zip(spellings, aliases, strict=True)
        ]

    assert results.count(True) == 1
    assert mock_notify.call_count == 1


def test_alert_without_ai_model_still_dedupes_on_raw_alias(db_session, test_user):
    """Callers without the resolved model keep the previous raw-alias contract."""
    account_id = str(test_user.account_id)

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        first = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai/some-model",
            provider_name="openai",
            total_tokens=10,
        )
        second = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="openai/some-model",
            provider_name="openai",
            total_tokens=10,
        )

    assert first is True and second is False
    assert mock_notify.call_count == 1


def test_different_models_sharing_a_bare_tail_alert_independently(
    db_session, test_user
):
    """A shared alias tail across providers must not swallow the second alert.

    ``openrouter/deepseek/deepseek-chat`` (OpenRouter routing to DeepSeek) and
    a direct ``deepseek-chat`` model both expose the bare tail
    ``deepseek-chat`` among their alias candidates. They are different
    provider configurations needing different price-catalog fixes, so the
    second model's admin alert must not be suppressed by the first model's
    cooldown.
    """
    from preloop.models.models.ai_model import AIModel

    account_id = str(test_user.account_id)
    via_openrouter = AIModel(
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-chat",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    direct = AIModel(
        provider_name="deepseek",
        model_identifier="deepseek-chat",
        api_endpoint="https://api.deepseek.com/v1",
    )

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        first = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="deepseek/deepseek-chat",
            provider_name="openrouter",
            total_tokens=100,
            ai_model=via_openrouter,
        )
        second = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="deepseek-chat",
            provider_name="deepseek",
            total_tokens=100,
            ai_model=direct,
        )

    assert first is True
    assert second is True
    assert mock_notify.call_count == 2


def test_endpoint_containing_openrouter_string_is_not_classified_openrouter(
    db_session, test_user
):
    """Provider classification must anchor on the URL host, not a substring.

    An unrelated upstream whose endpoint URL merely contains the string
    ``openrouter.ai`` (e.g. a path segment on a proxy) must NOT collapse into
    the ``openrouter`` dedup class: that would let a genuinely different
    upstream suppress OpenRouter's alert (or vice versa) within the cooldown.
    """
    from preloop.models.models.ai_model import AIModel

    account_id = str(test_user.account_id)
    via_openrouter = AIModel(
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-chat",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    lookalike = AIModel(
        provider_name="custom-proxy",
        model_identifier="deepseek/deepseek-chat",
        api_endpoint="https://llm.example.com/openrouter.ai/api/v1",
    )

    with patch.object(unpriced_model_alert, "notify_admins") as mock_notify:
        first = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="deepseek/deepseek-chat",
            provider_name="openrouter",
            total_tokens=100,
            ai_model=via_openrouter,
        )
        second = notify_unpriced_model(
            db_session,
            account_id=account_id,
            model_alias="deepseek/deepseek-chat",
            provider_name="custom-proxy",
            total_tokens=100,
            ai_model=lookalike,
        )

    assert first is True
    assert second is True
    assert mock_notify.call_count == 2
