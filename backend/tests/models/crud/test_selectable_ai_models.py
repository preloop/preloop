"""The selectable catalog preserves history but excludes retired built-ins."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_ai_model


@pytest.mark.parametrize("include_built_in", [True, False])
def test_selectable_catalog_filters_system_models(
    db_session: Session, test_user: models.User, include_built_in: bool
) -> None:
    own = models.AIModel(
        id=uuid4(),
        account_id=test_user.account_id,
        name="Own model",
        provider_name="openai",
        model_identifier="example-model",
    )
    foreign = models.AIModel(
        id=uuid4(),
        account_id=uuid4(),
        name="Foreign model",
        provider_name="openai",
        model_identifier="example-model",
    )
    from preloop.models.crud import crud_account

    foreign_account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Example foreign account",
            "is_active": True,
        },
    )
    foreign.account_id = foreign_account.id

    def system(alias: str, enabled: bool) -> models.AIModel:
        return models.AIModel(
            id=uuid4(),
            account_id=None,
            name=alias,
            provider_name="qwen",
            model_identifier="deepseek-v4.1-flash",
            meta_data={"gateway": {"enabled": enabled, "model_alias": alias}},
        )

    active = system("example/operator-model", True)
    retired = system("example/operator-model", False)
    legacy = system("openai/legacy-hosted", True)
    db_session.add_all([own, foreign, active, retired, legacy])
    db_session.flush()
    selected = crud_ai_model.get_selectable_for_account(
        db_session, account_id=test_user.account_id, include_built_in=include_built_in
    )
    selected_ids = {row.id for row in selected}
    assert own.id in selected_ids
    assert (active.id in selected_ids) is include_built_in
    assert (legacy.id in selected_ids) is include_built_in
    assert not {foreign.id, retired.id} & selected_ids
    assert crud_ai_model.get(db_session, id=retired.id) is not None


def test_builtin_usage_is_scoped_to_the_requesting_account(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    mocker: MockerFixture,
) -> None:
    """The same built-in ID never reveals another Cloud account's usage."""
    from datetime import UTC, datetime, timedelta
    from preloop.models.crud import crud_account

    mocker.patch(
        "preloop.api.endpoints.ai_models.built_in_models_enabled", return_value=True
    )
    other = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Other example account",
            "is_active": True,
        },
    )
    built_in = models.AIModel(
        id=uuid4(),
        name="DeepSeek V4.1 Flash",
        provider_name="qwen",
        model_identifier="deepseek-v4.1-flash",
        account_id=None,
        api_key="operator-private-key",
        is_default=True,
        meta_data={
            "gateway": {"enabled": True, "model_alias": "example/operator-model"}
        },
    )
    db_session.add(built_in)
    db_session.flush()
    for account_id, cost in [(test_user.account_id, 0.01), (other.id, 0.2)]:
        db_session.add(
            models.ApiUsage(
                id=uuid4(),
                user_id=test_user.id,
                account_id=account_id,
                endpoint="/openai/v1/chat/completions",
                method="POST",
                status_code=200,
                duration=0.1,
                action_type="model_gateway",
                ai_model_id=built_in.id,
                model_alias="example/operator-model",
                estimated_cost=cost,
                timestamp=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    db_session.commit()
    catalog = client.get("/api/v1/ai-models")
    assert catalog.status_code == 200
    assert [row["id"] for row in catalog.json()] == [str(built_in.id)]
    assert catalog.json()[0]["is_built_in"] is True
    for path in ("/summary", ""):
        response = client.get(f"/api/v1/ai-models/{built_in.id}{path}")
        assert response.status_code == 200
        if path:
            assert response.json()["total_requests"] == 1
            assert response.json()["estimated_cost"] == pytest.approx(0.01)
        else:
            assert "operator-private-key" not in response.text
    overview = client.get("/api/v1/ai-models/overview")
    assert overview.status_code == 200
    assert overview.json()["models"][0]["total_requests"] == 1
    # Permission middleware may refuse before the ownership-hiding 404.
    assert client.delete(f"/api/v1/ai-models/{built_in.id}").status_code in {403, 404}
    assert client.put(
        f"/api/v1/ai-models/{built_in.id}", json={"name": "Changed"}
    ).status_code in {403, 404}
    assert crud_ai_model.get(db_session, id=built_in.id).name == built_in.name


@pytest.mark.parametrize("own_default", [None, False, True])
@pytest.mark.parametrize("built_in_default", [False, True])
@pytest.mark.parametrize("own_kind", ["llm", "stt", "tts"])
def test_builtin_default_and_order_follow_account_configuration(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    mocker: MockerFixture,
    own_default: bool | None,
    built_in_default: bool,
    own_kind: str,
) -> None:
    """Account defaults override the built-in badge across catalog and detail."""
    mocker.patch(
        "preloop.api.endpoints.ai_models.built_in_models_enabled", return_value=True
    )
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other example account", "is_active": True},
    )
    db_session.add(
        models.AIModel(
            id=uuid4(),
            account_id=other.id,
            name="Other account default",
            provider_name="openai",
            model_identifier="example-model",
            is_default=True,
        )
    )
    owned = []
    if own_default is not None:
        # No credentials: configured defaults still override the fallback badge.
        owned.append(
            models.AIModel(
                id=uuid4(),
                account_id=test_user.account_id,
                name="Own model",
                provider_name="openai",
                model_identifier="example-model",
                meta_data={"service_kind": own_kind},
                is_default=own_default,
                created_at=datetime.now(UTC) - timedelta(minutes=2),
            )
        )
        owned.append(
            models.AIModel(
                id=uuid4(),
                account_id=test_user.account_id,
                name="Second model",
                provider_name="openai",
                model_identifier="another-model",
                is_default=False,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        db_session.add_all(owned)
        db_session.flush()
    built_in = models.AIModel(
        id=uuid4(),
        name="Built-in model",
        provider_name="qwen",
        model_identifier="deepseek-v4.1-flash",
        account_id=None,
        is_default=built_in_default,
        meta_data={
            "gateway": {"enabled": True, "model_alias": "example/operator-model"}
        },
    )
    db_session.add(built_in)
    db_session.commit()

    selected = crud_ai_model.get_selectable_for_account(
        db_session, account_id=test_user.account_id, include_built_in=True
    )
    expected_ids = [str(built_in.id), *[str(row.id) for row in owned]]
    assert [str(row.id) for row in selected] == expected_ids
    catalog = client.get("/api/v1/ai-models")
    assert catalog.status_code == 200
    assert [row["id"] for row in catalog.json()] == expected_ids
    expected_default = built_in_default and not (
        own_default is True and own_kind == "llm"
    )
    assert catalog.json()[0]["is_default"] is expected_default
    if owned:
        assert catalog.json()[1]["is_default"] is own_default
    detail = client.get(f"/api/v1/ai-models/{built_in.id}")
    assert detail.status_code == 200
    assert detail.json()["is_default"] is expected_default
    overview = client.get("/api/v1/ai-models/overview")
    assert overview.status_code == 200
    assert overview.json()["models"][0]["is_default"] is expected_default
    db_session.refresh(built_in)
    assert built_in.is_default is built_in_default
