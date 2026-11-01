"""CRUD for the weekly governance review tables.

Database access for these tables stays here. The Enterprise plugin calls
these methods; it does not query the models itself.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal, Mapping, Optional, Sequence, cast
from uuid import UUID, uuid4

from sqlalchemy import Integer, and_, func, literal, or_, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models import governance_review as gov

from .base import CRUDBase

GovernanceFinding = gov.GovernanceFinding
GovernancePolicyDocument = gov.GovernancePolicyDocument
GovernanceReviewRun = gov.GovernanceReviewRun
GovernanceReviewSettings = gov.GovernanceReviewSettings
GovernanceSessionDigest = gov.GovernanceSessionDigest
logger = logging.getLogger(__name__)
FINISHED_SUMMARY_STATUSES = (
    "complete",
    "deterministic_only",
    "deterministic_only_small",
)


def _digest_review_start() -> datetime:
    """Start of the review week ending at the latest Monday 07:00 UTC."""
    now = datetime.now(timezone.utc)
    cutoff = datetime.combine(
        (now - timedelta(days=now.weekday())).date(), time(7), tzinfo=timezone.utc
    )
    if cutoff > now:
        cutoff -= timedelta(days=7)
    return cutoff - timedelta(days=7)


def _digest_utc(value: datetime) -> datetime:
    """Normalize database timestamps for window enforcement."""
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


#: Sentinel for "leave this column unchanged" on an upsert.
_UNSET: Any = object()

#: Self-hosted installs set this to the analysis model alias. There is no
#: platform to fund the review, so the default stays off until it is set.
GOVERNANCE_ANALYSIS_MODEL_ENV = "PRELOOP_GOVERNANCE_ANALYSIS_MODEL"


class GovernanceReviewValidationError(ValueError):
    """A governance review write failed a column rule before it hit the database."""


def deployment_mode() -> Literal["cloud", "self-hosted"]:
    """Return ``cloud`` on the hosted service and ``self-hosted`` everywhere else.

    Hosted is ``PRELOOP_HOSTED``, the same switch ``instance_service`` uses.
    The import stays inside this function. ``instance_service`` imports
    ``httpx`` at module level, and this module is loaded by every worker
    that imports ``preloop.models.crud``.
    """
    from preloop.services.instance_service import is_hosted_instance

    if is_hosted_instance():
        return "cloud"
    return "self-hosted"


def default_review_mode() -> str:
    """Mode for an account that has no settings row.

    Implicit review is off on every edition. An explicitly configured
    governance analysis model is an independent operator opt-in; account
    provider keys and weekly email preferences do not enable analysis.

    Returns:
        One of the ``REVIEW_MODES`` values.
    """
    if os.getenv(GOVERNANCE_ANALYSIS_MODEL_ENV, "").strip():
        return gov.REVIEW_MODE_ON_OWN_MODEL
    return gov.REVIEW_MODE_OFF


def _json_size(value: Any) -> int:
    """UTF-8 size of a compact, key-sorted JSON rendering."""
    encoded = json.dumps(
        value, separators=(",", ":"), sort_keys=True, default=str
    ).encode("utf-8")
    return len(encoded)


def _require_mode(mode: str, *, field: str) -> str:
    if mode not in gov.REVIEW_MODES:
        raise GovernanceReviewValidationError(
            f"{field} must be one of {', '.join(gov.REVIEW_MODES)}"
        )
    return mode


def validate_digest(digest: Any) -> dict[str, Any]:
    """Check a spec 4.3 digest object.

    Unknown keys are kept so later summary fields can ride along. Known
    fields are checked when they are present. ``intent`` may be null on a
    deterministic-only segment.

    Args:
        digest: Candidate digest object.

    Returns:
        The same object, when it is valid.

    Raises:
        GovernanceReviewValidationError: The object breaks a digest rule.
    """
    if not isinstance(digest, dict):
        raise GovernanceReviewValidationError("digest must be an object")
    if _json_size(digest) > gov.DIGEST_MAX_BYTES:
        raise GovernanceReviewValidationError(
            f"digest exceeds {gov.DIGEST_MAX_BYTES} bytes"
        )
    if "summary_attempts" in digest and (
        type(digest["summary_attempts"]) is not int or digest["summary_attempts"] < 0
    ):
        raise GovernanceReviewValidationError(
            "digest.summary_attempts must be a non-negative integer"
        )
    intent = digest.get("intent")
    if intent is not None and (
        not isinstance(intent, str) or len(intent) > gov.DIGEST_INTENT_MAX_CHARS
    ):
        raise GovernanceReviewValidationError(
            f"digest.intent must be a string of at most "
            f"{gov.DIGEST_INTENT_MAX_CHARS} characters"
        )
    outcome = digest.get("outcome")
    if outcome is not None and outcome not in gov.DIGEST_OUTCOMES:
        raise GovernanceReviewValidationError(
            f"digest.outcome must be one of {', '.join(gov.DIGEST_OUTCOMES)}"
        )
    triviality = digest.get("triviality")
    if triviality is not None and triviality not in gov.DIGEST_TRIVIALITY:
        raise GovernanceReviewValidationError(
            "digest.triviality must be trivial, routine, or complex"
        )
    notable = digest.get("notable")
    if notable is not None:
        if not isinstance(notable, list) or len(notable) > gov.DIGEST_NOTABLE_MAX:
            raise GovernanceReviewValidationError(
                f"digest.notable must be a list of at most "
                f"{gov.DIGEST_NOTABLE_MAX} strings"
            )
        for item in notable:
            if not isinstance(item, str) or len(item) > gov.DIGEST_NOTABLE_MAX_CHARS:
                raise GovernanceReviewValidationError(
                    "digest.notable entries must be strings of at most "
                    f"{gov.DIGEST_NOTABLE_MAX_CHARS} characters"
                )
    return digest


def validate_observations(observations: Any) -> list[dict[str, Any]]:
    """Check the policy-aware observation list.

    Each item is ``{rule_ref, excerpt, confidence}``. ``excerpt`` is at most
    280 characters. Scrubbing is the caller's job; this only bounds the size.

    Args:
        observations: Candidate list.

    Returns:
        The same list, when it is valid.

    Raises:
        GovernanceReviewValidationError: An item is missing a field or the
            excerpt is too long.
    """
    if not isinstance(observations, list):
        raise GovernanceReviewValidationError("observations must be a list")
    for item in observations:
        if not isinstance(item, dict):
            raise GovernanceReviewValidationError("observation must be an object")
        rule_ref = item.get("rule_ref")
        if not isinstance(rule_ref, str) or not rule_ref:
            raise GovernanceReviewValidationError(
                "observation.rule_ref must be a non-empty string"
            )
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or (
            len(excerpt) > gov.OBSERVATION_EXCERPT_MAX_CHARS
        ):
            raise GovernanceReviewValidationError(
                "observation.excerpt must be a string of at most "
                f"{gov.OBSERVATION_EXCERPT_MAX_CHARS} characters"
            )
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float, str)):
            raise GovernanceReviewValidationError(
                "observation.confidence must be a number or a string"
            )
    return observations


def _validate_body(body_markdown: str) -> str:
    if not isinstance(body_markdown, str):
        raise GovernanceReviewValidationError("body_markdown must be a string")
    if len(body_markdown.encode("utf-8")) > gov.POLICY_DOCUMENT_BODY_MAX_BYTES:
        raise GovernanceReviewValidationError(
            f"body_markdown exceeds {gov.POLICY_DOCUMENT_BODY_MAX_BYTES} bytes"
        )
    return body_markdown


def _validate_source(source: str) -> str:
    if source not in gov.POLICY_DOCUMENT_SOURCES:
        raise GovernanceReviewValidationError(
            f"source must be one of {', '.join(gov.POLICY_DOCUMENT_SOURCES)}"
        )
    return source


def _validate_evidence(evidence: Any) -> list[dict[str, Any]]:
    if not isinstance(evidence, list):
        raise GovernanceReviewValidationError("evidence must be a list")
    for item in evidence:
        if not isinstance(item, dict):
            raise GovernanceReviewValidationError("evidence item must be an object")
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or (
            len(excerpt) > gov.FINDING_EVIDENCE_EXCERPT_MAX_CHARS
        ):
            raise GovernanceReviewValidationError(
                "evidence.excerpt must be a string of at most "
                f"{gov.FINDING_EVIDENCE_EXCERPT_MAX_CHARS} characters"
            )
        source = item.get("source")
        if not isinstance(source, str) or not source:
            raise GovernanceReviewValidationError(
                "evidence.source must be a non-empty string"
            )
    return evidence


def _as_uuid(value: Any, *, field: str) -> UUID:
    """Parse ``value`` as a UUID.

    Args:
        value: A UUID or a string UUID.
        field: Name used in the validation error.

    Returns:
        The parsed UUID.

    Raises:
        GovernanceReviewValidationError: ``value`` is not a UUID.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GovernanceReviewValidationError(f"{field} must be a UUID") from exc


def _require_session_in_account(
    db: Session, *, account_id: UUID, runtime_session_id: UUID
) -> None:
    """Reject a digest write whose session belongs to another account.

    Args:
        db: Database session.
        account_id: Account the caller claims to write for.
        runtime_session_id: Session the segment is attached to.

    Raises:
        GovernanceReviewValidationError: The session is missing or owned by
            a different account.
    """
    from preloop.models.models.runtime_session import RuntimeSession

    owner = (
        db.query(RuntimeSession.account_id)
        .filter(RuntimeSession.id == runtime_session_id)
        .scalar()
    )
    if owner is None:
        raise GovernanceReviewValidationError("runtime_session_id does not exist")
    if owner != account_id:
        raise GovernanceReviewValidationError(
            "runtime_session_id does not belong to this account"
        )


def _require_runs_in_account(
    db: Session, *, account_id: UUID, run_ids: set[UUID]
) -> None:
    """Reject findings whose run belongs to another account.

    Args:
        db: Database session.
        account_id: Account the caller claims to write for.
        run_ids: Run ids stamped on the findings.

    Raises:
        GovernanceReviewValidationError: A run is missing or owned by a
            different account.
    """
    if not run_ids:
        return
    owned = {
        row[0]
        for row in db.query(GovernanceReviewRun.id)
        .filter(
            GovernanceReviewRun.account_id == account_id,
            GovernanceReviewRun.id.in_(run_ids),
        )
        .all()
    }
    if run_ids - owned:
        raise GovernanceReviewValidationError("run_id does not belong to this account")


class CRUDGovernancePolicyDocument(CRUDBase[GovernancePolicyDocument]):
    """Create and read versioned governance policy documents."""

    def create_version(
        self,
        db: Session,
        *,
        account_id: UUID,
        body_markdown: str,
        source: str,
        created_by_id: Optional[UUID] = None,
        commit: bool = True,
    ) -> GovernancePolicyDocument:
        """Insert the next version and make it the only active one.

        An account-scoped advisory lock serializes two callers. The previous
        active row is cleared in the same transaction before the new row is
        inserted, and the partial unique index rejects a second active row
        if that lock is ever skipped.

        Args:
            db: Database session.
            account_id: Owning account.
            body_markdown: Policy prose, at most 64 KiB.
            source: ``default_template``, ``user``, or ``restore``.
            created_by_id: User who wrote this version, if any.
            commit: Commit when True, flush otherwise.

        Returns:
            The new active version.

        Raises:
            GovernanceReviewValidationError: The body or source is invalid.
        """
        _validate_body(body_markdown)
        _validate_source(source)
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"governance_policy_document:{account_id}"},
        )
        current = (
            db.query(func.max(GovernancePolicyDocument.version))
            .filter(GovernancePolicyDocument.account_id == account_id)
            .scalar()
        )
        db.query(GovernancePolicyDocument).filter(
            GovernancePolicyDocument.account_id == account_id,
            GovernancePolicyDocument.is_active.is_(True),
        ).update(
            {GovernancePolicyDocument.is_active: False},
            synchronize_session="fetch",
        )
        db.flush()
        row = GovernancePolicyDocument(
            account_id=account_id,
            version=(current or 0) + 1,
            body_markdown=body_markdown,
            is_active=True,
            created_by_id=created_by_id,
            source=source,
        )
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(row)
        return row

    def get_active(
        self, db: Session, *, account_id: UUID
    ) -> Optional[GovernancePolicyDocument]:
        """The account's active policy document, or None."""
        return (
            db.query(GovernancePolicyDocument)
            .filter(
                GovernancePolicyDocument.account_id == account_id,
                GovernancePolicyDocument.is_active.is_(True),
            )
            .first()
        )

    def list_history(
        self, db: Session, *, account_id: UUID
    ) -> list[GovernancePolicyDocument]:
        """Every version for the account, newest first."""
        return (
            db.query(GovernancePolicyDocument)
            .filter(GovernancePolicyDocument.account_id == account_id)
            .order_by(
                GovernancePolicyDocument.version.desc(),
                GovernancePolicyDocument.id.asc(),
            )
            .all()
        )


class CRUDGovernanceReviewSettings(CRUDBase[GovernanceReviewSettings]):
    """Read and set the per-account governance review mode."""

    def get_for_account(
        self, db: Session, *, account_id: UUID
    ) -> Optional[GovernanceReviewSettings]:
        """The stored settings row, or None when the account uses defaults."""
        return (
            db.query(GovernanceReviewSettings)
            .filter(GovernanceReviewSettings.account_id == account_id)
            .first()
        )

    def get_or_default(
        self, db: Session, *, account_id: UUID
    ) -> GovernanceReviewSettings:
        """The stored row, or an unsaved row carrying the deployment default.

        A missing row is not inserted. Every edition defaults to ``off``
        unless a governance analysis model is explicitly configured. Legacy
        rows without positive analysis consent receive a transient off view.

        Args:
            db: Database session.
            account_id: Owning account.

        Returns:
            The persisted row, or a transient row with ``mode`` set.
        """
        row = self.get_for_account(db, account_id=account_id)
        if row is not None:
            if row.mode != gov.REVIEW_MODE_OFF and not self.explicitly_enabled(
                db, row=row
            ):
                # Do not rewrite a stored customer choice. This transient view
                # fails closed for legacy automatically materialized settings.
                effective = GovernanceReviewSettings(
                    **{
                        column.name: getattr(row, column.name)
                        for column in GovernanceReviewSettings.__table__.columns
                    }
                )
                effective.mode = gov.REVIEW_MODE_OFF
                effective.stored_mode = row.mode
                effective.effective_reason = "explicit_analysis_opt_in_required"
                return effective
            return row
        return GovernanceReviewSettings(
            account_id=account_id,
            mode=default_review_mode(),
        )

    def explicitly_enabled(self, db: Session, *, row: GovernanceReviewSettings) -> bool:
        """Require positive governance-specific consent, never generic BYOK.

        Budget pauses can clear the last mode actor. The latest authenticated
        settings audit preserves the user's choice through these transitions.
        Missing/ambiguous legacy attribution is deliberately not consent.
        """
        if row.mode_changed_by_id is not None:
            return True
        if row.analysis_model_id is not None:
            return True
        if os.getenv(GOVERNANCE_ANALYSIS_MODEL_ENV, "").strip():
            return True
        latest = (
            db.query(models.AuditLog)
            .filter(
                models.AuditLog.account_id == row.account_id,
                models.AuditLog.action == "governance_review.settings_updated",
                models.AuditLog.status == "success",
                models.AuditLog.user_id.is_not(None),
            )
            .order_by(models.AuditLog.timestamp.desc(), models.AuditLog.id.desc())
            .first()
        )
        return latest is not None and (latest.details or {}).get("mode") in {
            gov.REVIEW_MODE_ON_PLATFORM,
            gov.REVIEW_MODE_ON_OWN_MODEL,
        }

    def set_mode(
        self,
        db: Session,
        *,
        account_id: UUID,
        mode: str,
        mode_changed_by_id: Optional[UUID] = None,
        analysis_model_id: Any = _UNSET,
        last_run_id: Any = _UNSET,
        commit: bool = True,
    ) -> GovernanceReviewSettings:
        """Create or update the account's mode in one statement.

        Args:
            db: Database session.
            account_id: Owning account.
            mode: One of ``REVIEW_MODES``.
            mode_changed_by_id: User who changed the mode, if any.
            analysis_model_id: Model to store. Omit to leave the column
                unchanged (null on insert).
            last_run_id: Run to store. Omit to leave the column unchanged.
            commit: Commit when True, flush otherwise.

        Returns:
            The stored settings row.

        Raises:
            GovernanceReviewValidationError: ``mode`` is not a known value.
        """
        _require_mode(mode, field="mode")
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "id": uuid4(),
            "account_id": account_id,
            "mode": mode,
            "mode_changed_at": now,
            "mode_changed_by_id": mode_changed_by_id,
        }
        updates: dict[str, Any] = {
            "mode": mode,
            "mode_changed_at": now,
            "mode_changed_by_id": mode_changed_by_id,
            "updated_at": func.now(),
        }
        if analysis_model_id is not _UNSET:
            values["analysis_model_id"] = analysis_model_id
            updates["analysis_model_id"] = analysis_model_id
        if last_run_id is not _UNSET:
            values["last_run_id"] = last_run_id
            updates["last_run_id"] = last_run_id
        statement = pg_insert(GovernanceReviewSettings).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_governance_review_settings_account",
            set_=updates,
        )
        db.execute(statement)
        if commit:
            db.commit()
        else:
            db.flush()
        row = self.get_for_account(db, account_id=account_id)
        assert row is not None
        db.refresh(row)
        return row

    def set_thresholds(
        self,
        db: Session,
        *,
        account_id: UUID,
        detector_thresholds: Optional[dict[str, Any]],
        commit: bool = True,
    ) -> GovernanceReviewSettings:
        """Store the review settings the measured checks read.

        A new row gets the deployment default mode. An existing row keeps
        its mode. The plugin validates the keys; this layer only bounds the
        stored value.

        Args:
            db: Database session.
            account_id: Owning account.
            detector_thresholds: Settings keyed by rule kind, or None to go
                back to the defaults.
            commit: Commit when True, flush otherwise.

        Returns:
            The stored settings row.

        Raises:
            GovernanceReviewValidationError: The value is not an object or
                is larger than ``DETECTOR_THRESHOLDS_MAX_BYTES``.
        """
        if detector_thresholds is not None:
            if not isinstance(detector_thresholds, dict):
                raise GovernanceReviewValidationError(
                    "detector_thresholds must be an object"
                )
            if _json_size(detector_thresholds) > gov.DETECTOR_THRESHOLDS_MAX_BYTES:
                raise GovernanceReviewValidationError(
                    "detector_thresholds exceeds "
                    f"{gov.DETECTOR_THRESHOLDS_MAX_BYTES} bytes"
                )
        statement = pg_insert(GovernanceReviewSettings).values(
            id=uuid4(),
            account_id=account_id,
            mode=default_review_mode(),
            detector_thresholds=detector_thresholds,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_governance_review_settings_account",
            set_={
                "detector_thresholds": statement.excluded.detector_thresholds,
                "updated_at": func.now(),
            },
        )
        db.execute(statement)
        if commit:
            db.commit()
        else:
            db.flush()
        row = self.get_for_account(db, account_id=account_id)
        assert row is not None
        db.refresh(row)
        return row


def _pair_digest(
    digest: Any, summary: Any
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Fill a missing digest or summary from the one that was provided.

    ``None`` and ``_UNSET`` both mean the caller omitted that column. When
    both are omitted the pair is left unchanged on conflict.

    Returns:
        The two objects to store, or None when both were omitted.
    """
    if digest is _UNSET:
        digest = None
    if summary is _UNSET:
        summary = None
    if digest is None and summary is None:
        return None
    if digest is None:
        copied = dict(summary or {})
        return copied, dict(copied)
    if summary is None:
        copied = dict(digest)
        return copied, dict(copied)
    return dict(digest), dict(summary)


def _assign_digest_fields(
    values: dict[str, Any],
    updates: dict[str, Any],
    *,
    policy_document_version: Any,
    user_id: Any,
    digest: Any,
    summary: Any,
    observations: Any,
    rolling_summary: Any,
    digest_version: Any,
    request_watermark: Any,
    model_alias: Any,
    input_tokens: Any,
    output_tokens: Any,
    estimated_cost: Any,
    funding: Any,
    truncated: Any,
) -> None:
    """Fill insert values, and conflict updates only for fields the caller set.

    Args:
        values: INSERT column map. Always receives a concrete value.
        updates: ON CONFLICT SET map. Gains a column only when the caller
            passed it.
        policy_document_version: Document version, or ``_UNSET``.
        user_id: Session user, or ``_UNSET``.
        digest: Spec 4.3 object, or ``_UNSET``.
        summary: Policy-aware object, or ``_UNSET``.
        observations: Observation list, or ``_UNSET``.
        rolling_summary: Rolling text, or ``_UNSET``.
        digest_version: Digest schema version, or ``_UNSET``.
        request_watermark: Covered request count, or ``_UNSET``.
        model_alias: Analysis model, or ``_UNSET``.
        input_tokens: Input token count, or ``_UNSET``.
        output_tokens: Output token count, or ``_UNSET``.
        estimated_cost: Analysis cost, or ``_UNSET``.
        funding: ``platform`` or ``account``, or ``_UNSET``.
        truncated: Sample-cut flag, or ``_UNSET``.

    Raises:
        GovernanceReviewValidationError: A provided value breaks a column rule.
    """

    def keep(column: str, provided: Any, default: Any) -> None:
        if provided is _UNSET:
            values[column] = default
            return
        values[column] = provided
        updates[column] = provided

    if policy_document_version is _UNSET:
        values["policy_document_version"] = None
    else:
        if policy_document_version is not None and policy_document_version < 1:
            raise GovernanceReviewValidationError(
                "policy_document_version must be >= 1"
            )
        values["policy_document_version"] = policy_document_version
        updates["policy_document_version"] = policy_document_version
    keep("user_id", user_id, None)

    paired = _pair_digest(digest, summary)
    if paired is None:
        values["digest"] = {}
        values["summary"] = {}
    else:
        stored_digest, stored_summary = paired
        validate_digest(stored_digest)
        validate_digest(stored_summary)
        values["digest"] = stored_digest
        values["summary"] = stored_summary
        updates["digest"] = stored_digest
        updates["summary"] = stored_summary

    if observations is _UNSET:
        values["observations"] = []
    else:
        stored_observations = validate_observations(observations or [])
        values["observations"] = stored_observations
        updates["observations"] = stored_observations

    keep("rolling_summary", rolling_summary, None)
    keep("digest_version", digest_version, 1)
    keep("request_watermark", request_watermark, 0)
    keep("model_alias", model_alias, None)
    keep("input_tokens", input_tokens, 0)
    keep("output_tokens", output_tokens, 0)
    keep("estimated_cost", estimated_cost, 0.0)
    if funding is not _UNSET and funding not in gov.DIGEST_FUNDING:
        raise GovernanceReviewValidationError(
            f"funding must be one of {', '.join(gov.DIGEST_FUNDING)}"
        )
    keep("funding", funding, gov.DIGEST_FUNDING_PLATFORM)
    keep("truncated", truncated, False)


class CRUDGovernanceSessionDigest(CRUDBase[GovernanceSessionDigest]):
    """Upsert session segments and list those in a review window."""

    def upsert(
        self,
        db: Session,
        *,
        account_id: UUID,
        runtime_session_id: UUID,
        segment_index: int = 0,
        window_start: datetime,
        window_end: datetime,
        policy_document_version: Any = _UNSET,
        user_id: Any = _UNSET,
        digest: Any = _UNSET,
        summary: Any = _UNSET,
        observations: Any = _UNSET,
        rolling_summary: Any = _UNSET,
        digest_version: Any = _UNSET,
        request_watermark: Any = _UNSET,
        model_alias: Any = _UNSET,
        input_tokens: Any = _UNSET,
        output_tokens: Any = _UNSET,
        estimated_cost: Any = _UNSET,
        funding: Any = _UNSET,
        truncated: Any = _UNSET,
        commit: bool = True,
        force_resummarize: bool = False,
        include_history: bool = False,
        override_caller: str | None = None,
        override_reason: str | None = None,
        review_window_start: datetime | None = None,
    ) -> GovernanceSessionDigest:
        """Insert or replace one segment of a session.

        The key is ``(runtime_session_id, segment_index)``. Passing only
        ``digest`` or only ``summary`` copies that object into the other
        column so a caller that knows one name still fills both.

        Omitted optional columns stay as they are on conflict. An insert
        uses the column default (empty observations, zero tokens and cost,
        ``platform`` funding, digest version 1). ``account_id`` is written
        on insert and never rewritten: the session must already belong to
        that account.

        Args:
            db: Database session.
            account_id: Owning account.
            runtime_session_id: Session this segment belongs to.
            segment_index: Zero-based segment number.
            window_start: Inclusive start of the summarised span.
            window_end: End of the summarised span.
            policy_document_version: Active document version, if one existed.
            user_id: User the session belongs to, if known.
            digest: Spec 4.3 structured record.
            summary: Policy-aware generic digest. Defaults to ``digest``.
            observations: Structured policy observations.
            rolling_summary: Text the next segment and Stage B should read.
            digest_version: Schema version of ``digest``.
            request_watermark: Request count covered by this segment.
            model_alias: Analysis model, when a model call was made.
            input_tokens: Analysis input tokens.
            output_tokens: Analysis output tokens.
            estimated_cost: Analysis cost.
            funding: ``platform`` or ``account``.
            truncated: True when the transcript sample was cut to fit.
            commit: Commit when True, flush otherwise.
            force_resummarize: Permit replacing a completed segment explicitly.
            include_history: Permit creating or summarizing earlier content.
            override_caller: Required caller identity for either override.
            override_reason: Required reason, written to the audit log.
            review_window_start: Optional narrower review boundary. Cannot
                widen the default week ending at the latest Monday 07:00 UTC
                without the explicit include_history override.

        Returns:
            The stored segment.

        Raises:
            GovernanceReviewValidationError: A digest, observation, funding
                value, or session owner is invalid.
        """
        overrides = force_resummarize or include_history
        if overrides and (
            not override_caller
            or not override_caller.strip()
            or not override_reason
            or not override_reason.strip()
        ):
            raise GovernanceReviewValidationError(
                "summary overrides require a caller and reason"
            )
        paired = _pair_digest(digest, summary)
        if paired is not None:
            digest, summary = paired
        review_start = _digest_review_start()
        if review_window_start is not None:
            # A narrower caller window is useful; an older boundary must not
            # become an unaudited substitute for include_history.
            review_start = max(review_start, _digest_utc(review_window_start))
        if not include_history and _digest_utc(window_start) < review_start:
            # Retiring a failed historical span sends no content to a model and
            # must not require permission to analyze that history. Never allow
            # this diagnostic transition to create a historical segment.
            existing = self.get_multi(
                db,
                account_id=str(account_id),
                runtime_session_id=runtime_session_id,
                segment_index=segment_index,
                limit=1,
            )
            retiring = (
                isinstance(digest, dict)
                and digest.get("summary_status") == "outside_review_window"
                and existing
                and _digest_utc(existing[0].window_start) == _digest_utc(window_start)
                and _digest_utc(existing[0].window_end) == _digest_utc(window_end)
            )
            if not retiring:
                raise GovernanceReviewValidationError(
                    "segment lies outside the review window; include_history is required"
                )
        if segment_index < 0:
            raise GovernanceReviewValidationError("segment_index must be >= 0")
        if window_end < window_start:
            raise GovernanceReviewValidationError(
                "window_end must be at or after window_start"
            )
        _require_session_in_account(
            db, account_id=account_id, runtime_session_id=runtime_session_id
        )
        values: dict[str, Any] = {
            "id": uuid4(),
            "account_id": account_id,
            "runtime_session_id": runtime_session_id,
            "segment_index": segment_index,
            "window_start": window_start,
            "window_end": window_end,
        }
        # ``account_id`` is not part of the conflict key, so a re-upsert must
        # not move the segment to another account.
        updates: dict[str, Any] = {
            "window_start": window_start,
            "window_end": window_end,
            "updated_at": func.now(),
        }
        _assign_digest_fields(
            values,
            updates,
            policy_document_version=policy_document_version,
            user_id=user_id,
            digest=digest,
            summary=summary,
            observations=observations,
            rolling_summary=rolling_summary,
            digest_version=digest_version,
            request_watermark=request_watermark,
            model_alias=model_alias,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            funding=funding,
            truncated=truncated,
        )
        status = GovernanceSessionDigest.digest["summary_status"].astext
        intent = GovernanceSessionDigest.digest["intent"].astext
        writable = or_(
            and_(status.is_not(None), status.notin_(FINISHED_SUMMARY_STATUSES)),
            and_(status.is_(None), or_(intent.is_(None), func.btrim(intent) == "")),
        )
        if "digest" in updates:
            # A caller's stale snapshot cannot buy another retry allowance by
            # resetting a failed segment's durable provider-attempt counter.
            incoming = updates["digest"]
            stored_attempts = func.coalesce(
                GovernanceSessionDigest.digest["summary_attempts"].astext.cast(Integer),
                0,
            )
            incoming_attempts = incoming.get("summary_attempts", 0)
            same_claim_refund = and_(
                status == "summarizing",
                incoming.get("summary_status") == "summarizing",
                GovernanceSessionDigest.digest["summary_attempted_at"].astext
                == incoming.get("summary_attempted_at"),
                incoming.get("summary_attempted_at") is not None,
                incoming.get("provider_status_code") == 400,
                incoming.get("summary_stop_reason") == "response_format_rejected",
                stored_attempts == incoming_attempts + 1,
            )
            retiring = and_(
                incoming.get("summary_status") == "outside_review_window",
                GovernanceSessionDigest.window_start == window_start,
                GovernanceSessionDigest.window_end == window_end,
            )
            if incoming.get("summary_status") == "outside_review_window":
                # Retirement spends nothing, but must retain the durable count
                # even when the caller supplies only a minimal diagnostic.
                for column in ("digest", "summary"):
                    updates[column] = func.jsonb_set(
                        literal(updates[column], type_=JSONB),
                        text("'{summary_attempts}'::text[]"),
                        func.to_jsonb(
                            func.greatest(stored_attempts, incoming_attempts)
                        ),
                    )
            writable = and_(
                writable,
                or_(stored_attempts <= incoming_attempts, same_claim_refund, retiring),
            )
        statement = pg_insert(GovernanceSessionDigest).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_governance_session_digest_segment",
            set_=updates,
            where=None if force_resummarize else writable,
        ).returning(GovernanceSessionDigest.id)
        identifier = db.execute(statement).scalar_one_or_none()
        if identifier is None:
            raise GovernanceReviewValidationError(
                "cannot overwrite a complete segment or reset summary attempts without force_resummarize"
            )
        if overrides:
            from preloop.services.session_search_index import redact_text
            from .audit_log import crud_audit_log

            assert override_caller is not None and override_reason is not None
            details = {
                "caller": redact_text(override_caller.strip())[0][:160],
                "reason": redact_text(override_reason.strip())[0][:600],
                "force_resummarize": force_resummarize,
                "include_history": include_history,
                "segment_index": segment_index,
                "runtime_session_id": str(runtime_session_id),
            }
            audit = crud_audit_log.log_action(
                db,
                account_id=account_id,
                action="governance_review.summary_override",
                resource_type="governance_session_digest",
                resource_id=str(identifier),
                status="success",
                details=details,
                commit=False,
            )
            logger.warning(
                "Governance digest write override account=%s segment=%s audit_event=%s force_resummarize=%s include_history=%s",
                account_id,
                identifier,
                audit.id,
                force_resummarize,
                include_history,
            )
        if commit:
            db.commit()
        else:
            db.flush()
        row = (
            db.query(GovernanceSessionDigest)
            .filter(
                GovernanceSessionDigest.runtime_session_id == runtime_session_id,
                GovernanceSessionDigest.segment_index == segment_index,
            )
            .one()
        )
        db.refresh(row)
        return row

    def update(
        self,
        db: Session,
        *,
        db_obj: GovernanceSessionDigest,
        obj_in: dict[str, Any],
        commit: bool = True,
    ) -> GovernanceSessionDigest:
        """Keep generic CRUD updates subject to the same atomic write guard.

        Exceptional replacements use upsert's explicit, audited overrides.
        Identity and account ownership cannot change through this shortcut.
        """
        identity = {"id", "account_id", "runtime_session_id", "segment_index"}
        if any(
            key in obj_in and obj_in[key] != getattr(db_obj, key) for key in identity
        ):
            raise GovernanceReviewValidationError("segment identity cannot change")
        timestamps = {
            key: value
            for key, value in obj_in.items()
            if key in {"created_at", "updated_at"}
        }
        fields = {
            key: value
            for key, value in obj_in.items()
            if key not in identity and key not in timestamps
        }
        if not fields:
            return super().update(db, db_obj=db_obj, obj_in=timestamps, commit=commit)
        fields.setdefault("window_start", db_obj.window_start)
        fields.setdefault("window_end", db_obj.window_end)
        stored = self.upsert(
            db,
            account_id=db_obj.account_id,
            runtime_session_id=db_obj.runtime_session_id,
            segment_index=db_obj.segment_index,
            commit=False,
            **fields,
        )
        return super().update(db, db_obj=stored, obj_in=timestamps, commit=commit)

    def list_for_window(
        self,
        db: Session,
        *,
        account_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[GovernanceSessionDigest]:
        """Segments whose ``window_end`` falls in ``[window_start, window_end)``.

        Half-open so a segment that ends on the next week's boundary is not
        counted twice. Ordered by session, then segment.
        """
        return (
            db.query(GovernanceSessionDigest)
            .filter(GovernanceSessionDigest.account_id == account_id)
            .filter(GovernanceSessionDigest.window_end >= window_start)
            .filter(GovernanceSessionDigest.window_end < window_end)
            .order_by(
                GovernanceSessionDigest.runtime_session_id.asc(),
                GovernanceSessionDigest.segment_index.asc(),
            )
            .all()
        )


class CRUDGovernanceReviewRun(CRUDBase[GovernanceReviewRun]):
    """Create an idempotent weekly run and finish it."""

    def list_runs(
        self, db: Session, *, account_id: UUID, limit: int = 25, offset: int = 0
    ) -> list[GovernanceReviewRun]:
        """Return an account's run history, newest review window first."""
        return (
            db.query(GovernanceReviewRun)
            .filter(GovernanceReviewRun.account_id == account_id)
            .order_by(GovernanceReviewRun.window_start.desc(), GovernanceReviewRun.id)
            .offset(offset)
            .limit(limit)
            .all()
        )

    def segment_counts(
        self, db: Session, *, account_id: UUID, run_ids: Sequence[UUID]
    ) -> dict[UUID, int]:
        """Count stored segments in each run's half-open review window.

        Counts describe available data, including deterministic-only segments.
        Reviewed-session coverage comes from the run's persisted counters.
        """
        if not run_ids:
            return {}
        rows = (
            db.query(GovernanceReviewRun.id, func.count(GovernanceSessionDigest.id))
            .outerjoin(
                GovernanceSessionDigest,
                (GovernanceSessionDigest.account_id == GovernanceReviewRun.account_id)
                & (
                    GovernanceSessionDigest.window_end
                    >= GovernanceReviewRun.window_start
                )
                & (GovernanceSessionDigest.window_end < GovernanceReviewRun.window_end),
            )
            .filter(
                GovernanceReviewRun.account_id == account_id,
                GovernanceReviewRun.id.in_(run_ids),
            )
            .group_by(GovernanceReviewRun.id)
            .all()
        )
        return {run_id: count for run_id, count in rows}

    def summaries_started_at(
        self, db: Session, *, account_id: UUID
    ) -> Optional[datetime]:
        """Return the first stored segment date, or None before the first sweep."""
        return cast(
            Optional[datetime],
            db.query(func.min(GovernanceSessionDigest.created_at))
            .filter(GovernanceSessionDigest.account_id == account_id)
            .scalar(),
        )

    def create_run(
        self,
        db: Session,
        *,
        account_id: UUID,
        window_start: datetime,
        window_end: datetime,
        started_at: Optional[datetime] = None,
        policy_document_id: Optional[UUID] = None,
        mode_before: Optional[str] = None,
        status: str = gov.RUN_STATUS_RUNNING,
        commit: bool = True,
    ) -> GovernanceReviewRun:
        """Insert a run, or return the one already stored for this week.

        The unique key is ``(account_id, window_start)``. A second call does
        not reset status, costs, or timestamps.

        Args:
            db: Database session.
            account_id: Owning account.
            window_start: Review window start. Part of the idempotency key.
            window_end: Review window end.
            started_at: When this attempt started. Defaults to now.
            policy_document_id: Active document, if any.
            mode_before: Mode at the start of the run.
            status: Initial status. Ignored when the run already exists.
            commit: Commit when True, flush otherwise.

        Returns:
            The existing or newly inserted run.

        Raises:
            GovernanceReviewValidationError: A status, mode, or window is
                invalid.
        """
        if status not in gov.RUN_STATUSES:
            raise GovernanceReviewValidationError(
                f"status must be one of {', '.join(gov.RUN_STATUSES)}"
            )
        if mode_before is not None:
            _require_mode(mode_before, field="mode_before")
        if window_end < window_start:
            raise GovernanceReviewValidationError(
                "window_end must be at or after window_start"
            )
        statement = (
            pg_insert(GovernanceReviewRun)
            .values(
                id=uuid4(),
                account_id=account_id,
                window_start=window_start,
                window_end=window_end,
                policy_document_id=policy_document_id,
                status=status,
                mode_before=mode_before,
                started_at=started_at or datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                constraint="uq_governance_review_run_account_window"
            )
        )
        db.execute(statement)
        if commit:
            db.commit()
        else:
            db.flush()
        row = (
            db.query(GovernanceReviewRun)
            .filter(
                GovernanceReviewRun.account_id == account_id,
                GovernanceReviewRun.window_start == window_start,
            )
            .one()
        )
        db.refresh(row)
        return row

    def finish_run(
        self,
        db: Session,
        *,
        account_id: UUID,
        run_id: UUID,
        status: str,
        finished_at: Optional[datetime] = None,
        commit: bool = True,
        **fields: Any,
    ) -> Optional[GovernanceReviewRun]:
        """Mark a run finished and store the counters the caller passes.

        Args:
            db: Database session.
            account_id: Owning account. A run from another account is ignored.
            run_id: Run to finish.
            status: Terminal or current status.
            finished_at: When the run finished. Defaults to now.
            commit: Commit when True, flush otherwise.
            **fields: Optional columns: ``skip_reason``,
                ``estimated_cost_preflight``, ``platform_cost``,
                ``account_cost``, ``sessions_considered``,
                ``sessions_summarized``, ``findings_count``, ``mode_after``,
                ``policy_document_id``.

        Returns:
            The updated run, or None when this account has no such run.

        Raises:
            GovernanceReviewValidationError: The status, mode, or a field
                name is invalid.
        """
        allowed = {
            "skip_reason",
            "estimated_cost_preflight",
            "platform_cost",
            "account_cost",
            "sessions_considered",
            "sessions_summarized",
            "findings_count",
            "mode_after",
            "policy_document_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise GovernanceReviewValidationError(
                f"unknown finish fields: {', '.join(sorted(unknown))}"
            )
        if status not in gov.RUN_STATUSES:
            raise GovernanceReviewValidationError(
                f"status must be one of {', '.join(gov.RUN_STATUSES)}"
            )
        mode_after = fields.get("mode_after", _UNSET)
        if mode_after is not _UNSET and mode_after is not None:
            _require_mode(mode_after, field="mode_after")
        row = (
            db.query(GovernanceReviewRun)
            .filter(
                GovernanceReviewRun.id == run_id,
                GovernanceReviewRun.account_id == account_id,
            )
            .first()
        )
        if row is None:
            return None
        row.status = status
        row.finished_at = finished_at or datetime.now(timezone.utc)
        for key, value in fields.items():
            setattr(row, key, value)
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(row)
        return row


class CRUDGovernanceFinding(CRUDBase[GovernanceFinding]):
    """Insert findings once per fingerprint, list them, and dismiss them."""

    def member_names(
        self, db: Session, *, account_id: UUID, user_ids: Sequence[UUID]
    ) -> dict[UUID, str]:
        """Resolve finding authors and dismissers in one account-scoped read."""
        if not user_ids:
            return {}
        rows = (
            db.query(models.User.id, models.User.full_name, models.User.username)
            .filter(models.User.account_id == account_id, models.User.id.in_(user_ids))
            .all()
        )
        return {user_id: full_name or username for user_id, full_name, username in rows}

    def insert_findings(
        self,
        db: Session,
        *,
        account_id: UUID,
        findings: Sequence[Mapping[str, Any]],
        commit: bool = True,
    ) -> list[GovernanceFinding]:
        """Insert findings, skipping any fingerprint this account already has.

        A fingerprint repeated inside ``findings`` is inserted at most once.
        Existing fingerprints are a no-op and are not returned.

        Args:
            db: Database session.
            account_id: Owning account. Stamped on every row. Each ``run_id``
                must already belong to this account.
            findings: Finding payloads. Each needs ``run_id``, ``kind``,
                ``category``, ``severity``, ``title``, ``suggestion``, and
                ``fingerprint``.
            commit: Commit when True, flush otherwise.

        Returns:
            The rows this call inserted, in input order.

        Raises:
            GovernanceReviewValidationError: A payload breaks a finding rule,
                an identifier is not a UUID, or ``run_id`` belongs to
                another account.
        """
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in findings:
            fingerprint = raw.get("fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                raise GovernanceReviewValidationError(
                    "fingerprint must be a non-empty string"
                )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            rows.append(self._row(account_id, raw))
        if not rows:
            return []
        _require_runs_in_account(
            db,
            account_id=account_id,
            run_ids={row["run_id"] for row in rows},
        )
        statement = (
            pg_insert(GovernanceFinding)
            .values(rows)
            .on_conflict_do_nothing(
                constraint="uq_governance_finding_account_fingerprint"
            )
            .returning(GovernanceFinding.id)
        )
        inserted_ids = list(db.execute(statement).scalars().all())
        if commit:
            db.commit()
        else:
            db.flush()
        if not inserted_ids:
            return []
        stored = (
            db.query(GovernanceFinding)
            .filter(GovernanceFinding.id.in_(inserted_ids))
            .all()
        )
        by_id = {row.id: row for row in stored}
        return [by_id[row_id] for row_id in inserted_ids if row_id in by_id]

    def _row(self, account_id: UUID, raw: Mapping[str, Any]) -> dict[str, Any]:
        kind = raw.get("kind")
        if kind not in gov.FINDING_KINDS:
            raise GovernanceReviewValidationError(
                f"kind must be one of {', '.join(gov.FINDING_KINDS)}"
            )
        severity = raw.get("severity")
        if severity not in gov.FINDING_SEVERITIES:
            raise GovernanceReviewValidationError(
                f"severity must be one of {', '.join(gov.FINDING_SEVERITIES)}"
            )
        title = raw.get("title")
        if not isinstance(title, str) or not title:
            raise GovernanceReviewValidationError("title must be a non-empty string")
        if len(title) > gov.FINDING_TITLE_MAX_CHARS:
            raise GovernanceReviewValidationError(
                f"title must be at most {gov.FINDING_TITLE_MAX_CHARS} characters"
            )
        suggestion = raw.get("suggestion")
        if not isinstance(suggestion, str) or not suggestion:
            raise GovernanceReviewValidationError(
                "suggestion must be a non-empty string"
            )
        if len(suggestion) > gov.FINDING_SUGGESTION_MAX_CHARS:
            raise GovernanceReviewValidationError(
                "suggestion must be at most "
                f"{gov.FINDING_SUGGESTION_MAX_CHARS} characters"
            )
        category = raw.get("category")
        if not isinstance(category, str) or not category or len(category) > 64:
            raise GovernanceReviewValidationError(
                "category must be a string of at most 64 characters"
            )
        session_ids = [
            _as_uuid(value, field="session_ids")
            for value in raw.get("session_ids") or []
        ]
        if len(session_ids) > gov.FINDING_SESSION_IDS_MAX:
            raise GovernanceReviewValidationError(
                f"session_ids must contain at most {gov.FINDING_SESSION_IDS_MAX} ids"
            )
        evidence = _validate_evidence(raw.get("evidence") or [])
        rule_ref = raw.get("rule_ref")
        if rule_ref is not None and (
            not isinstance(rule_ref, str) or len(rule_ref) > 255
        ):
            raise GovernanceReviewValidationError(
                "rule_ref must be a string of at most 255 characters"
            )
        run_id = raw.get("run_id")
        if run_id is None:
            raise GovernanceReviewValidationError("run_id is required")
        return {
            "id": uuid4(),
            "account_id": account_id,
            "run_id": _as_uuid(run_id, field="run_id"),
            "kind": kind,
            "category": category,
            "severity": severity,
            "title": title,
            "evidence": evidence,
            "session_ids": session_ids,
            "user_id": (
                _as_uuid(raw["user_id"], field="user_id")
                if raw.get("user_id") is not None
                else None
            ),
            "agent_ref": raw.get("agent_ref"),
            "suggestion": suggestion,
            "estimated_weekly_savings": raw.get("estimated_weekly_savings"),
            "fingerprint": raw["fingerprint"],
            "rule_ref": rule_ref,
        }

    def list_findings(
        self,
        db: Session,
        *,
        account_id: UUID,
        kind: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        user_id: Optional[UUID] = None,
        run_id: Optional[UUID] = None,
        dismissed: Optional[bool] = None,
        allowed_kinds: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[GovernanceFinding]:
        """List findings for an account.

        Args:
            db: Database session.
            account_id: Owning account.
            kind: Filter to one kind.
            category: Filter to one measured category; tool_hygiene aliases unused_tools.
            severity: Filter to one severity.
            user_id: Filter to one user.
            run_id: Filter to one run.
            dismissed: True for dismissed rows, False for open rows, None
                for both.
            allowed_kinds: Permission scope. An empty sequence returns no rows.
            limit: Maximum rows, or None for existing unpaged callers.
            offset: Rows to skip after applying every filter.

        Returns:
            Matching findings, oldest first. Permission filtering precedes paging.
        """
        query = db.query(GovernanceFinding).filter(
            GovernanceFinding.account_id == account_id
        )
        if allowed_kinds is not None:
            query = query.filter(GovernanceFinding.kind.in_(allowed_kinds))
        if category is not None:
            query = query.filter(
                GovernanceFinding.category
                == ("unused_tools" if category == "tool_hygiene" else category)
            )
        if kind is not None:
            query = query.filter(GovernanceFinding.kind == kind)
        if severity is not None:
            query = query.filter(GovernanceFinding.severity == severity)
        if user_id is not None:
            query = query.filter(GovernanceFinding.user_id == user_id)
        if run_id is not None:
            query = query.filter(GovernanceFinding.run_id == run_id)
        if dismissed is True:
            query = query.filter(GovernanceFinding.dismissed_at.is_not(None))
        elif dismissed is False:
            query = query.filter(GovernanceFinding.dismissed_at.is_(None))
        query = query.order_by(
            GovernanceFinding.created_at.asc(),
            GovernanceFinding.id.asc(),
        ).offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return query.all()

    def dismiss(
        self,
        db: Session,
        *,
        account_id: UUID,
        finding_id: UUID,
        dismissed_by_id: Optional[UUID],
        reason: Optional[str] = None,
        dismissed_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[GovernanceFinding]:
        """Record a dismissal on one finding.

        Args:
            db: Database session.
            account_id: Owning account.
            finding_id: Finding to dismiss.
            dismissed_by_id: User who dismissed it.
            reason: Why it was dismissed.
            dismissed_at: When. Defaults to now.
            commit: Commit when True, flush otherwise.

        Returns:
            The updated finding, or None when this account has no such row.
        """
        row = (
            db.query(GovernanceFinding)
            .filter(
                GovernanceFinding.id == finding_id,
                GovernanceFinding.account_id == account_id,
            )
            .first()
        )
        if row is None:
            return None
        row.dismissed_by_id = dismissed_by_id
        row.dismissed_at = dismissed_at or datetime.now(timezone.utc)
        row.dismissed_reason = reason
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(row)
        return row


crud_governance_policy_document = CRUDGovernancePolicyDocument(GovernancePolicyDocument)
crud_governance_review_settings = CRUDGovernanceReviewSettings(GovernanceReviewSettings)
crud_governance_session_digest = CRUDGovernanceSessionDigest(GovernanceSessionDigest)
crud_governance_review_run = CRUDGovernanceReviewRun(GovernanceReviewRun)
crud_governance_finding = CRUDGovernanceFinding(GovernanceFinding)
