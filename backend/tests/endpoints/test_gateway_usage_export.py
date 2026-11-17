"""The usage export is account scoped and contains controls, never bodies."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import account


def test_export_scopes_the_query_and_reports_serving_host() -> None:
    app = FastAPI()
    app.include_router(account.router, prefix="/api/v1")
    owner = SimpleNamespace(id="account-1")
    app.dependency_overrides[account.get_account_for_user] = lambda: owner
    app.dependency_overrides[account.get_current_active_user] = lambda: SimpleNamespace(
        account_id="account-1"
    )
    app.dependency_overrides[account.get_db_session] = lambda: MagicMock()
    with (
        patch(
            "preloop.services.gateway_usage_export.crud_api_usage.export_gateway_rows",
            return_value=[
                {
                    "id": "usage-1",
                    "timestamp": datetime.now(UTC),
                    "upstream_provider": "Host A",
                    "upstream_request_id": "gen-example",
                }
            ],
        ) as query,
        patch(
            "preloop.services.gateway_usage_export.restrict_history_window",
            side_effect=lambda db, **kw: (kw["start_date"], kw["end_date"]),
        ),
    ):
        response = TestClient(app).get("/api/v1/account/gateway-usage/export")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "Host A" in response.text
    assert "gen-example" in response.text
    assert query.call_args.kwargs["account_id"] == "account-1"


def test_export_refuses_truncation_and_reversed_windows() -> None:
    """An export must never quietly present a partial interval as complete."""
    import pytest
    from datetime import timedelta
    from fastapi import HTTPException
    from preloop.services.gateway_usage_export import export_gateway_usage

    now = datetime.now(UTC)
    with pytest.raises(HTTPException) as error:
        export_gateway_usage(
            MagicMock(),
            account=SimpleNamespace(id="account-1"),
            start_date=now,
            end_date=now - timedelta(seconds=1),
        )
    assert error.value.status_code == 422
    with (
        patch("preloop.services.gateway_usage_export.MAX_EXPORT_ROWS", 1),
        patch(
            "preloop.services.gateway_usage_export.crud_api_usage.export_gateway_rows",
            return_value=[{}, {}],
        ),
        patch(
            "preloop.services.gateway_usage_export.restrict_history_window",
            return_value=(now - timedelta(days=1), now),
        ),
        pytest.raises(HTTPException) as error,
    ):
        export_gateway_usage(
            MagicMock(),
            account=SimpleNamespace(id="account-1"),
            start_date=None,
            end_date=None,
        )
    assert error.value.status_code == 413


def test_csv_formula_cells_and_unknown_metadata_are_not_executable_or_exported() -> (
    None
):
    from preloop.services.gateway_usage_export import gateway_usage_csv

    result = gateway_usage_csv(
        [
            {
                "upstream_provider": "=1+2",
                "upstream_parameters": {
                    "model": "example/model",
                    "headers": {"Authorization": "private-secret"},
                    "messages": ["private text"],
                    "extra_body": {"model": "private-override"},
                    "provider": {"only": ["host-a"]},
                },
            }
        ]
    )
    assert "'=1+2" in result
    assert "private" not in result
    assert "host-a" in result
