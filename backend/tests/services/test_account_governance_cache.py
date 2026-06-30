"""Unit tests for the short-lived account subject-governance cache."""

from unittest.mock import patch

import pytest

from preloop.models.crud import crud_account
from preloop.services import account_governance_cache as cache_mod
from preloop.services.account_governance_cache import (
    account_subject_governance_store_is_empty,
    clear_account_governance_cache,
    get_cached_account_meta_data,
    invalidate_account_governance_cache,
)
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    set_subject_governance,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts and ends with an empty module-level cache."""
    clear_account_governance_cache()
    yield
    clear_account_governance_cache()


def _populate_account_governance(db_session, account_id):
    account = crud_account.get(db_session, id=account_id)
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-1",
        config={"allowed_models": ["openai/gpt-5"]},
    )
    db_session.add(account)
    db_session.commit()
    return account


# --- account_subject_governance_store_is_empty -----------------------------


def test_store_is_empty_for_none():
    assert account_subject_governance_store_is_empty(None) is True


def test_store_is_empty_for_empty_dict():
    assert account_subject_governance_store_is_empty({}) is True


def test_store_is_empty_when_buckets_have_no_config():
    meta_data = {"subject_governance": {"api_keys": {}, "managed_agents": {}}}
    assert account_subject_governance_store_is_empty(meta_data) is True


def test_store_is_empty_ignores_non_dict_buckets():
    meta_data = {"subject_governance": {"api_keys": "garbage"}}
    assert account_subject_governance_store_is_empty(meta_data) is True


def test_store_is_empty_ignores_empty_config_dict():
    meta_data = {"subject_governance": {"api_keys": {"key-1": {}}}}
    assert account_subject_governance_store_is_empty(meta_data) is True


def test_store_not_empty_when_config_present():
    meta_data = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-1",
        config={"allowed_models": ["openai/gpt-5"]},
    )
    assert account_subject_governance_store_is_empty(meta_data) is False


# --- get_cached_account_meta_data ------------------------------------------


def test_get_cached_returns_none_for_empty_account(db_session, test_user):
    result = get_cached_account_meta_data(db_session, str(test_user.account_id))
    assert result is None


def test_get_cached_returns_meta_for_populated_account(db_session, test_user):
    _populate_account_governance(db_session, test_user.account_id)
    result = get_cached_account_meta_data(db_session, str(test_user.account_id))
    assert result is not None
    assert "subject_governance" in result


def test_get_cached_returns_none_for_missing_account(db_session):
    import uuid

    result = get_cached_account_meta_data(db_session, str(uuid.uuid4()))
    assert result is None


def test_negative_result_is_cached_and_skips_db(db_session, test_user):
    account_id = str(test_user.account_id)
    # First call populates the negative cache entry.
    get_cached_account_meta_data(db_session, account_id)
    with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
        result = get_cached_account_meta_data(db_session, account_id)
        assert result is None
        get_mock.assert_not_called()


def test_positive_result_is_cached_and_skips_db(db_session, test_user):
    account_id = str(test_user.account_id)
    _populate_account_governance(db_session, test_user.account_id)
    get_cached_account_meta_data(db_session, account_id)
    with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
        result = get_cached_account_meta_data(db_session, account_id)
        assert result is not None
        get_mock.assert_not_called()


def test_invalidate_forces_db_refetch(db_session, test_user):
    account_id = str(test_user.account_id)
    get_cached_account_meta_data(db_session, account_id)
    invalidate_account_governance_cache(account_id)
    with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
        get_cached_account_meta_data(db_session, account_id)
        get_mock.assert_called_once()


def test_clear_cache_drops_all_entries(db_session, test_user):
    account_id = str(test_user.account_id)
    get_cached_account_meta_data(db_session, account_id)
    clear_account_governance_cache()
    with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
        get_cached_account_meta_data(db_session, account_id)
        get_mock.assert_called_once()


def test_cache_expires_after_ttl(db_session, test_user):
    account_id = str(test_user.account_id)
    base = 1000.0
    with patch.object(cache_mod.time, "monotonic", return_value=base):
        get_cached_account_meta_data(db_session, account_id)
    # Jump past the TTL window so the cached entry is treated as stale.
    expired = base + cache_mod._TTL_SECONDS + 1
    with patch.object(cache_mod.time, "monotonic", return_value=expired):
        with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
            get_cached_account_meta_data(db_session, account_id)
            get_mock.assert_called_once()


def test_uuid_and_str_account_id_share_cache_entry(db_session, test_user):
    # The cache keys on str(account_id), so a UUID and its string form collide.
    get_cached_account_meta_data(db_session, str(test_user.account_id))
    with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
        get_cached_account_meta_data(db_session, test_user.account_id)
        get_mock.assert_not_called()
