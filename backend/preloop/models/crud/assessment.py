"""Append-only Delivery Report assessments and their history reads (#2016)."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.assessment import ASSESSMENT_SOURCES, ASSESSMENT_VERDICTS

from .base import CRUDBase


class CRUDAssessment(CRUDBase[models.Assessment]):
    """Write assessments and read the newest one per subject and revision."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        source: str,
        purpose_verdict: str,
        issue_id: Optional[UUID] = None,
        issue_cost_pull_request_id: Optional[UUID] = None,
        flow_execution_id: Optional[UUID] = None,
        revision: Optional[str] = None,
        criteria_snapshot: Optional[Any] = None,
        risk_flags: Optional[list[Any]] = None,
        evidence: Optional[dict[str, Any]] = None,
        evaluator_version: Optional[str] = None,
        assessed_at: Optional[datetime] = None,
        cost: Optional[Decimal] = None,
    ) -> models.Assessment:
        """Append one assessment; earlier rows are never changed.

        Args:
            db: Database session.
            account_id: Owning account.
            source: One of ``ASSESSMENT_SOURCES``.
            purpose_verdict: One of ``ASSESSMENT_VERDICTS``.
            issue_id: Assessed issue, if any.
            issue_cost_pull_request_id: Assessed pull request, if any.
            flow_execution_id: The audit or report run that wrote it.
            revision: Commit SHA the assessment is pinned to.
            criteria_snapshot: The criteria as read at assessment time.
            risk_flags: Likely regression classes.
            evidence: Supporting evidence.
            evaluator_version: Version of the evaluator that produced it.
            assessed_at: When it was assessed; defaults to the database time.
            cost: What the assessment cost, in USD.

        Returns:
            The flushed assessment.

        Raises:
            ValueError: Unknown source or verdict, or no subject.
        """
        if source not in ASSESSMENT_SOURCES:
            raise ValueError(f"unknown assessment source {source!r}")
        if purpose_verdict not in ASSESSMENT_VERDICTS:
            raise ValueError(f"unknown purpose verdict {purpose_verdict!r}")
        if issue_id is None and issue_cost_pull_request_id is None:
            raise ValueError("an assessment needs an issue or a pull request")
        row = models.Assessment(
            account_id=account_id,
            issue_id=issue_id,
            issue_cost_pull_request_id=issue_cost_pull_request_id,
            flow_execution_id=flow_execution_id,
            source=source,
            revision=revision,
            criteria_snapshot=criteria_snapshot,
            purpose_verdict=purpose_verdict,
            risk_flags=risk_flags or [],
            evidence=evidence or {},
            evaluator_version=evaluator_version,
            cost=cost,
        )
        if assessed_at is not None:
            row.assessed_at = assessed_at
        db.add(row)
        db.flush()
        db.refresh(row)
        return row

    def _for_subject(
        self,
        account_id: UUID,
        issue_id: Optional[UUID],
        issue_cost_pull_request_id: Optional[UUID],
    ) -> Select[models.Assessment]:
        if (issue_id is None) == (issue_cost_pull_request_id is None):
            raise ValueError("name exactly one of issue_id or pull request id")
        subject = (
            models.Assessment.issue_id == issue_id
            if issue_id is not None
            else models.Assessment.issue_cost_pull_request_id
            == issue_cost_pull_request_id
        )
        return select(models.Assessment).where(
            models.Assessment.account_id == account_id, subject
        )

    def newest(
        self,
        db: Session,
        *,
        account_id: UUID,
        revision: Optional[str],
        issue_id: Optional[UUID] = None,
        issue_cost_pull_request_id: Optional[UUID] = None,
    ) -> Optional[models.Assessment]:
        """The newest assessment of one subject at one revision."""
        query = self._for_subject(account_id, issue_id, issue_cost_pull_request_id)
        query = query.where(
            models.Assessment.revision.is_(None)
            if revision is None
            else models.Assessment.revision == revision
        )
        return db.scalar(
            query.order_by(
                models.Assessment.assessed_at.desc(), models.Assessment.id.desc()
            ).limit(1)
        )

    def newest_per_revision(
        self,
        db: Session,
        *,
        account_id: UUID,
        issue_id: Optional[UUID] = None,
        issue_cost_pull_request_id: Optional[UUID] = None,
    ) -> list[models.Assessment]:
        """The newest assessment of one subject for each revision, newest first.

        Name exactly one of ``issue_id`` or ``issue_cost_pull_request_id``.
        """
        rank = (
            func.row_number()
            .over(
                partition_by=models.Assessment.revision,
                order_by=(
                    models.Assessment.assessed_at.desc(),
                    models.Assessment.id.desc(),
                ),
            )
            .label("rank")
        )
        ranked = (
            self._for_subject(account_id, issue_id, issue_cost_pull_request_id)
            .with_only_columns(models.Assessment.id, rank)
            .subquery()
        )
        return list(
            db.scalars(
                select(models.Assessment)
                .where(
                    models.Assessment.id.in_(
                        select(ranked.c.id).where(ranked.c.rank == 1)
                    )
                )
                .order_by(
                    models.Assessment.assessed_at.desc(), models.Assessment.id.desc()
                )
            )
        )


crud_assessment = CRUDAssessment(models.Assessment)
