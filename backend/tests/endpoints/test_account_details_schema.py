"""Schema tests for account details, including default_runner_pool."""

from preloop.api.endpoints.account import (
    AccountDetailsResponse,
    AccountDetailsUpdate,
)


def test_account_details_update_normalizes_default_runner_pool() -> None:
    assert (
        AccountDetailsUpdate(default_runner_pool="  office-mac  ").default_runner_pool
        == "office-mac"
    )
    assert AccountDetailsUpdate(default_runner_pool="").default_runner_pool is None
    assert AccountDetailsUpdate(default_runner_pool=None).default_runner_pool is None


def test_account_details_response_includes_default_runner_pool() -> None:
    body = AccountDetailsResponse(
        id="11111111-1111-4111-8111-111111111111",
        organization_name="Example Org",
        default_runner_pool="server",
        created_at="2026-09-04T00:00:00",
        updated_at="2026-09-04T00:00:00",
    )
    assert body.default_runner_pool == "server"
    dumped = body.model_dump()
    assert dumped["default_runner_pool"] == "server"
