"""Tests for account gateway usage summary endpoint defaults."""

import inspect

from fastapi.params import Query as QueryParam

from preloop.api.endpoints.account import get_account_gateway_usage_summary


def test_gateway_usage_summary_include_breakdown_defaults_false() -> None:
    """Card/summary callers get totals only unless they opt into breakdown."""
    signature = inspect.signature(get_account_gateway_usage_summary)
    param = signature.parameters["include_breakdown"]
    default = param.default

    assert isinstance(default, QueryParam)
    assert default.default is False
