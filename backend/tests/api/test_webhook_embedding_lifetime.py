"""Embedding providers must not borrow the API's database capacity."""

from collections import Counter
from typing import Any
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.sync.tasks import _generate_webhook_embeddings
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_tracker,
    crud_organization,
    crud_project,
    crud_issue,
    crud_embedding_model,
    crud_issue_embedding,
)


def test_owned_embedding_session_releases_connection_and_persists_result(
    db_engine: Any,
    monkeypatch: Any,
) -> None:
    unique = str(uuid4())
    with Session(db_engine) as db:
        account = crud_account.create(db, obj_in={"organization_name": unique})
        account_id = account.id
        tracker = crud_tracker.create(
            db,
            obj_in={
                "name": unique,
                "tracker_type": "gitlab",
                "account_id": account_id,
                "api_key": "synthetic",
                "url": "https://tracker.invalid",
                "is_active": True,
            },
        )
        tracker_id = tracker.id
        organization = crud_organization.create(
            db,
            obj_in={
                "name": unique,
                "identifier": unique,
                "tracker_id": tracker_id,
            },
        )
        project = crud_project.create(
            db,
            obj_in={
                "name": unique,
                "identifier": unique,
                "organization_id": organization.id,
            },
        )
        issue = crud_issue.create(
            db,
            obj_in={
                "title": "Synthetic embedding",
                "description": "body",
                "external_id": unique,
                "project_id": project.id,
                "tracker_id": tracker_id,
            },
        )
        issue_id = issue.id
        model = crud_embedding_model.create(
            db,
            obj_in={
                "name": unique,
                "provider": "synthetic",
                "version": unique,
                "dimensions": 1536,
                "is_active": True,
            },
        )
        model_id = model.id
        companion = crud_embedding_model.create(
            db,
            obj_in={
                "name": f"{unique}-companion",
                "provider": "synthetic",
                "version": f"{unique}-companion",
                "dimensions": 1536,
                "is_active": True,
            },
        )
        companion_id = companion.id
        # Migrations may seed active models. The worker intentionally embeds
        # with every active model, not only the ones created by this test.
        expected_calls = Counter(
            {active.name: 2 for active in crud_embedding_model.get_active(db)}
        )

    calls: list[str] = []

    def generate(*, text: str, model: Any, **kwargs: Any) -> list[float]:
        # A real SQLAlchemy pool, not a mocked Session.close assertion.
        assert db_engine.pool.checkedout() == 0
        calls.append(model.name)
        return [0.1] * 1536

    monkeypatch.setattr(crud_issue_embedding, "_generate_embedding_vector", generate)
    try:
        with Session(db_engine) as dependency:
            # Exercise both initial insert and existing-row update after commit
            # expiry, ensuring no ORM refresh happens during provider work.
            for _ in range(2):
                _generate_webhook_embeddings(
                    dependency, {"issue_id": issue_id, "force_update": True}
                )
        assert Counter(calls) == expected_calls
        with Session(db_engine) as db:
            embeddings = crud_issue_embedding.get_for_issue_content(
                db, issue_id=issue_id
            )
            assert set(embeddings) == set(expected_calls)
            assert embeddings[f"{unique}-companion"].embedding_model_id == companion_id
            assert embeddings[unique].embedding_model_id == model_id
            assert len(embeddings[unique].embedding) == 1536
    finally:
        with Session(db_engine) as db:
            crud_account.delete(db, id=account_id)
            crud_embedding_model.delete(db, id=model_id)
            crud_embedding_model.delete(db, id=companion_id)


def test_connection_releasing_embedding_mode_rejects_unrelated_pending_writes() -> None:
    with Session() as db:
        db.add(models.Account(organization_name="uncommitted caller work"))
        with pytest.raises(ValueError, match="clean owned session"):
            crud_issue_embedding.create_embeddings(
                db, issue_id=str(uuid4()), release_connection_for_generation=True
            )


def test_connection_releasing_embedding_mode_rejects_flushed_caller_transaction(
    db_engine: Any,
    monkeypatch: Any,
) -> None:
    with Session(db_engine) as db:
        unrelated = models.Account(organization_name="flushed caller work")
        db.add(unrelated)
        db.flush()
        account_id = unrelated.id
        assert not db.new and not db.dirty and not db.deleted
        lookup = MagicMock(side_effect=AssertionError("must reject before any lookup"))
        commit = MagicMock(side_effect=AssertionError("must not commit caller work"))
        generate = MagicMock(side_effect=AssertionError("must not call provider"))
        monkeypatch.setattr(db, "get", lookup)
        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr(
            crud_issue_embedding, "_generate_embedding_vector", generate
        )
        with pytest.raises(ValueError, match="clean owned session"):
            crud_issue_embedding.create_embeddings(
                db, issue_id=str(uuid4()), release_connection_for_generation=True
            )
        assert db.in_transaction()
        lookup.assert_not_called()
        commit.assert_not_called()
        generate.assert_not_called()
    with Session(db_engine) as db:
        assert crud_account.get(db, id=account_id) is None
