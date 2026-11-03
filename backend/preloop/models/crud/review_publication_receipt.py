"""Receipts for pull request reviews a code host accepted (EE #202)."""

from __future__ import annotations

from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase


def normalize_repository(value: Any) -> str:
    """Lower-cased ``owner/repo`` (or project path) without slashes at the ends."""
    return str(value or "").strip().strip("/").lower()


class CRUDReviewPublicationReceipt(CRUDBase[models.ReviewPublicationReceipt]):
    """Write and read review publication receipts."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID | str,
        execution_id: UUID | str,
        provider: str,
        repository: str,
        pull_request: str | int,
        review_action: str,
        head_sha: Optional[str] = None,
        provider_review_id: Optional[str | int] = None,
    ) -> models.ReviewPublicationReceipt:
        """Persist and commit one receipt. Raises on failure; callers log it."""
        row = models.ReviewPublicationReceipt(
            account_id=UUID(str(account_id)),
            execution_id=UUID(str(execution_id)),
            provider=str(provider).lower(),
            repository=normalize_repository(repository),
            pull_request=str(pull_request),
            review_action=str(review_action).lower(),
            head_sha=(str(head_sha).lower() if head_sha else None),
            provider_review_id=(
                str(provider_review_id) if provider_review_id is not None else None
            ),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_for_execution(
        self, db: Session, *, account_id: UUID | str, execution_id: UUID | str
    ) -> List[models.ReviewPublicationReceipt]:
        """Receipts of one execution in one account, oldest first."""
        model = models.ReviewPublicationReceipt
        return (
            db.query(model)
            .filter(
                model.account_id == UUID(str(account_id)),
                model.execution_id == UUID(str(execution_id)),
            )
            .order_by(model.created_at.asc())
            .all()
        )

    def find_for_target(
        self,
        db: Session,
        *,
        account_id: UUID | str,
        repository: str,
        pull_request: str | int,
        head_sha: str,
    ) -> Optional[models.ReviewPublicationReceipt]:
        """A receipt from any run for the same pull request and head commit."""
        model = models.ReviewPublicationReceipt
        return (
            db.query(model)
            .filter(
                model.account_id == UUID(str(account_id)),
                model.repository == normalize_repository(repository),
                model.pull_request == str(pull_request),
                model.head_sha == str(head_sha).lower(),
            )
            .order_by(model.created_at.desc())
            .first()
        )


crud_review_publication_receipt = CRUDReviewPublicationReceipt(
    models.ReviewPublicationReceipt
)
