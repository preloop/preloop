"""Tests for ``CRUDApiUsage.get_by_ids`` validation and bounds."""

from uuid import uuid4

from preloop.models.crud import crud_api_usage
from preloop.models.crud.api_usage import _MAX_GET_BY_IDS


def test_get_by_ids_skips_invalid_uuids_and_dedupes(db_session, test_user) -> None:
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.01,
    )
    db_session.commit()

    rows = crud_api_usage.get_by_ids(
        db_session,
        ids=[str(usage.id), "not-a-uuid", str(usage.id), None],  # type: ignore[list-item]
        account_id=test_user.account_id,
    )
    assert len(rows) == 1
    assert rows[0].id == usage.id


def test_get_by_ids_caps_query_size(db_session, test_user) -> None:
    created = []
    for _ in range(3):
        created.append(
            crud_api_usage.log_gateway_request(
                db_session,
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=200,
                duration=0.1,
                user_id=str(test_user.id),
                account_id=str(test_user.account_id),
                model_alias="openai/gpt-test",
                provider_name="openai",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost=0.0,
            )
        )
    db_session.commit()

    oversized = [str(row.id) for row in created] + [
        str(uuid4()) for _ in range(_MAX_GET_BY_IDS)
    ]
    rows = crud_api_usage.get_by_ids(
        db_session,
        ids=oversized,
        account_id=test_user.account_id,
    )
    assert len(rows) <= _MAX_GET_BY_IDS
    # First unique ids win the cap; the three created rows are included.
    returned_ids = {row.id for row in rows}
    assert {row.id for row in created} <= returned_ids
