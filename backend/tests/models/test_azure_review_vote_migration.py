"""Migration compatibility for existing Azure Repos voting preferences."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_tracker
from preloop.utils.azure_devops import validate_azure_devops_config


def test_existing_vote_default_preserves_explicit_preferences(
    db_session: Session, test_user: models.User
) -> None:
    """Backfill only legacy Azure trackers, preserving other settings and opt-outs."""
    rows = []
    for tracker_type, details in (
        ("azure_devops", {"organization": "example-org"}),
        (
            "azure_devops",
            {"organization": "example-org", "cast_token_owner_vote": False},
        ),
        (
            "azure_devops",
            {"organization": "example-org", "cast_token_owner_vote": True},
        ),
        ("github", {"repository": "example/repo"}),
        ("azure_devops", None),
    ):
        rows.append(
            crud_tracker.create(
                db_session,
                obj_in={
                    "name": "Example tracker",
                    "tracker_type": tracker_type,
                    "account_id": test_user.account_id,
                    "connection_details": details,
                },
            )
        )
    path = (
        Path(__file__).parents[2]
        / "preloop/models/alembic/versions/20261229_azure_review_vote.py"
    )
    spec = importlib.util.spec_from_file_location("azure_review_vote", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.upgrade()
        migration.upgrade()
    for row in rows:
        db_session.refresh(row)
    assert [row.connection_details.get("cast_token_owner_vote") for row in rows] == [
        True,
        False,
        True,
        None,
        True,
    ]
    assert rows[0].connection_details["organization"] == "example-org"
    new = validate_azure_devops_config(
        api_key="synthetic-token",
        auth_type="api_token",
        connection_details={"organization": "example-org"},
    )
    assert new["cast_token_owner_vote"] is False
