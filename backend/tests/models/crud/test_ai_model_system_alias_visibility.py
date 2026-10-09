"""Operator warning counts do not expose another account's identifiers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from preloop.models.crud import crud_ai_model


def test_system_alias_collision_counts_are_aggregate_only() -> None:
    def row(account: str, alias: str) -> SimpleNamespace:
        return SimpleNamespace(
            account_id=account,
            provider_name="example",
            model_identifier="model",
            meta_data={"gateway": {"model_alias": alias, "enabled": True}},
        )

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        row("account-one", "example/shared"),
        row("account-one", "example/shared"),
        row("account-two", "example/shared"),
        row("account-other", "unrelated"),
        SimpleNamespace(
            account_id="account-disabled",
            provider_name="example",
            model_identifier="model",
            meta_data={"gateway": {"model_alias": "example/shared", "enabled": False}},
        ),
    ]
    counts = crud_ai_model.system_alias_collision_counts(db, alias="example/shared")
    assert counts == {"model_count": 3, "account_count": 2}
    assert "account-one" not in str(counts)
