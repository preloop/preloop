"""Principal-owned subscriptions and fresh trusted completion eligibility."""

from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.ci_principal import CiAuthorizationContext, crud_ci_principal
from preloop.schemas.ci_execution import CiReviewBinding
from preloop.schemas.ci_principal import CiAction
from preloop.schemas.ci_subscription import (
    COMPLETION_EVENT,
    CiSubscriptionBinding,
    CiSubscriptionCreate,
    CiSubscriptionUpdate,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.services.event_webhooks.targets import blocked_target_reason
from preloop.utils.encryption import encrypt_value

_STABLE_FIELDS = (
    "account_id",
    "principal_id",
    "project_id",
    "flow_id",
    "tracker_id",
    "repository_identifier",
    "repository_slug",
    "tracker_type",
    "tracker_url",
)


def is_ci_endpoint(endpoint: models.WebhookEndpoint) -> bool:
    """Partial machine attribution is restricted, never human fallback."""
    return any(
        getattr(endpoint, name, None) is not None
        for name in (
            "ci_principal_id",
            "initiating_ci_key_id",
            "ci_subscription_binding",
        )
    )


def _snapshot(context: CiAuthorizationContext) -> CiSubscriptionBinding:
    values = {name: getattr(context, name) for name in (*_STABLE_FIELDS, "key_id")}
    return CiSubscriptionBinding(version=1, **values)


def _context(binding: CiSubscriptionBinding) -> CiAuthorizationContext:
    values = binding.model_dump()
    values.pop("version")
    return CiAuthorizationContext(actions=frozenset(), **values)


def _binding(endpoint: models.WebhookEndpoint) -> CiSubscriptionBinding:
    binding = CiSubscriptionBinding.model_validate(endpoint.ci_subscription_binding)
    if (
        endpoint.ci_principal_id != binding.principal_id
        or endpoint.account_id != binding.account_id
        or (
            endpoint.initiating_ci_key_id is not None
            and endpoint.initiating_ci_key_id != binding.key_id
        )
        or endpoint.event_types != [COMPLETION_EVENT]
        or endpoint.source != "account"
        or endpoint.approval_workflow_id is not None
        or endpoint.created_by_user_id is not None
    ):
        raise ValueError("Invalid CI subscription attribution")
    return binding


def _validate_url(url: str) -> None:
    """Retain the established webhook target protections."""
    if blocked_target_reason(url):
        raise ValueError("Webhook target is not allowed")


class CRUDCiSubscription:
    """No history inference and no initiating-key authority for delivery."""

    def create(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        payload: CiSubscriptionCreate,
        max_endpoints: int = 20,
    ) -> tuple[models.WebhookEndpoint, str]:
        """Persist a strict, server-bound endpoint and return its secret once."""
        fresh = crud_ci_principal.authorize(
            db, context=context, action=CiAction.CREATE_SUBSCRIPTION
        )
        payload = CiSubscriptionCreate.model_validate(payload)
        _validate_url(payload.url)
        if (
            db.query(models.WebhookEndpoint)
            .filter(
                models.WebhookEndpoint.account_id == fresh.account_id,
                models.WebhookEndpoint.source == "account",
            )
            .count()
            >= max_endpoints
        ):
            raise ValueError("Webhook endpoint limit reached")
        secret = generate_secret()
        endpoint = models.WebhookEndpoint(
            account_id=fresh.account_id,
            ci_principal_id=fresh.principal_id,
            initiating_ci_key_id=fresh.key_id,
            ci_subscription_binding=_snapshot(fresh).model_dump(mode="json"),
            url=payload.url,
            description=payload.description,
            active=payload.active,
            event_types=[COMPLETION_EVENT],
            source="account",
            secret_encrypted=encrypt_value(secret),
            secret_hint=secret_hint(secret),
        )
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        return endpoint, secret

    def list_owned(
        self, db: Session, *, context: CiAuthorizationContext
    ) -> list[models.WebhookEndpoint]:
        """List only freshly authorized, structurally valid owned subscriptions."""
        fresh = crud_ci_principal.authorize(
            db, context=context, action=CiAction.READ_SUBSCRIPTION
        )
        rows = (
            db.query(models.WebhookEndpoint)
            .populate_existing()
            .filter(
                models.WebhookEndpoint.account_id == fresh.account_id,
                models.WebhookEndpoint.ci_principal_id == fresh.principal_id,
            )
            .order_by(models.WebhookEndpoint.created_at.desc())
            .all()
        )
        result = []
        for row in rows:
            try:
                binding = _binding(row)
                expected = _snapshot(fresh)
                if all(
                    getattr(binding, name) == getattr(expected, name)
                    for name in _STABLE_FIELDS
                ):
                    result.append(row)
            except (ValidationError, ValueError, TypeError):
                continue
        return result

    def get_owned(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        endpoint_id: UUID,
        action: CiAction = CiAction.READ_SUBSCRIPTION,
    ) -> models.WebhookEndpoint:
        """Read an owned subscription after fresh key/grant/resource authorization."""
        fresh = crud_ci_principal.authorize(db, context=context, action=action)
        endpoint = (
            db.query(models.WebhookEndpoint)
            .populate_existing()
            .filter(
                models.WebhookEndpoint.id == endpoint_id,
                models.WebhookEndpoint.account_id == fresh.account_id,
                models.WebhookEndpoint.ci_principal_id == fresh.principal_id,
            )
            .first()
        )
        try:
            binding = _binding(endpoint) if endpoint is not None else None
            expected = _snapshot(fresh)
            if binding is None or not all(
                getattr(binding, name) == getattr(expected, name)
                for name in _STABLE_FIELDS
            ):
                raise ValueError("Not owned")
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise LookupError("Webhook endpoint not found") from None
        assert endpoint is not None
        return endpoint

    def update_owned(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        endpoint_id: UUID,
        payload: CiSubscriptionUpdate,
    ) -> models.WebhookEndpoint:
        """Change receiver settings while retaining immutable event/resource scope."""
        payload = CiSubscriptionUpdate.model_validate(
            payload.model_dump(exclude_unset=True)
        )
        endpoint = self.get_owned(
            db,
            context=context,
            endpoint_id=endpoint_id,
            action=CiAction.UPDATE_SUBSCRIPTION,
        )
        fields = payload.model_dump(exclude_unset=True)
        fields.pop("event_types", None)
        if "url" in fields:
            _validate_url(fields["url"])
        for name, value in fields.items():
            setattr(endpoint, name, value)
        if "url" in fields or fields.get("active") is True:
            endpoint.consecutive_failures = 0
            endpoint.circuit_opened_at = None
        db.commit()
        db.refresh(endpoint)
        return endpoint

    def remove_owned(
        self, db: Session, *, context: CiAuthorizationContext, endpoint_id: UUID
    ) -> None:
        """Delete an owned endpoint and its cascading delivery history."""
        endpoint = self.get_owned(
            db,
            context=context,
            endpoint_id=endpoint_id,
            action=CiAction.DELETE_SUBSCRIPTION,
        )
        db.delete(endpoint)
        db.commit()

    def rotate_secret(
        self, db: Session, *, context: CiAuthorizationContext, endpoint_id: UUID
    ) -> tuple[models.WebhookEndpoint, str]:
        """Rotate an owned receiver secret and return the plaintext once."""
        endpoint = self.get_owned(
            db,
            context=context,
            endpoint_id=endpoint_id,
            action=CiAction.ROTATE_SUBSCRIPTION_SECRET,
        )
        return self._rotate(db, endpoint)

    def rotate_human_secret(
        self, db: Session, *, account_id: UUID, endpoint_id: UUID
    ) -> tuple[models.WebhookEndpoint, str]:
        """Retain administrator rotation for account-scoped manual endpoints."""
        endpoint = (
            db.query(models.WebhookEndpoint)
            .filter(
                models.WebhookEndpoint.id == endpoint_id,
                models.WebhookEndpoint.account_id == account_id,
                models.WebhookEndpoint.source == "account",
            )
            .first()
        )
        if endpoint is None:
            raise LookupError("Webhook endpoint not found")
        return self._rotate(db, endpoint)

    def _rotate(
        self, db: Session, endpoint: models.WebhookEndpoint
    ) -> tuple[models.WebhookEndpoint, str]:
        """Store fresh encrypted signing material without retaining plaintext."""
        secret = generate_secret()
        endpoint.secret_encrypted = encrypt_value(secret)
        endpoint.secret_hint = secret_hint(secret)
        db.commit()
        db.refresh(endpoint)
        return endpoint, secret

    def callback_payload(
        self,
        db: Session,
        *,
        endpoint: models.WebhookEndpoint,
        account_id: Any,
        event_type: str,
        subject_id: Any,
    ) -> dict[str, Any] | None:
        """Use only persisted terminal execution attribution, never emitter data."""
        if not is_ci_endpoint(endpoint) or event_type != COMPLETION_EVENT:
            return None
        try:
            db.refresh(endpoint)
            if not endpoint.active:
                return None
            binding = _binding(endpoint)
            if str(account_id) != str(binding.account_id):
                return None
            crud_ci_principal.authorize_principal(
                db, context=_context(binding), action=CiAction.CREATE_SUBSCRIPTION
            )
            execution_id = UUID(str(subject_id))
            execution = (
                db.query(models.FlowExecution)
                .populate_existing()
                .filter(
                    models.FlowExecution.id == execution_id,
                    models.FlowExecution.ci_principal_id == binding.principal_id,
                )
                .first()
            )
            if execution is None or execution.status not in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "STOPPED",
            }:
                return None
            review = CiReviewBinding.model_validate(execution.ci_review_binding)
            if (
                execution.flow_id != review.flow_id
                or execution.ci_principal_id != review.principal_id
                or (
                    execution.initiating_ci_key_id is not None
                    and execution.initiating_ci_key_id != review.key_id
                )
                or not all(
                    getattr(review, name) == getattr(binding, name)
                    for name in _STABLE_FIELDS
                )
            ):
                return None
            return {
                "execution_id": str(execution.id),
                "flow_id": str(review.flow_id),
                "project_id": str(review.project_id),
                "repository_identifier": review.repository_identifier,
                "pr_number": review.pr_number,
                "provider_pr_id": review.provider_pr_id,
                "head_sha": review.head_sha,
                "status": execution.status,
                "result_ready": execution.result is not None,
            }
        except (
            PermissionError,
            ValidationError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            return None

    def replay_allowed(self, db: Session, *, endpoint_id: UUID) -> bool:
        """Generic administrator replay never posts restricted machine events."""
        endpoint = (
            db.query(models.WebhookEndpoint)
            .filter(models.WebhookEndpoint.id == endpoint_id)
            .first()
        )
        return endpoint is not None and not is_ci_endpoint(endpoint)

    def prepare_delivery(
        self, db: Session, *, delivery_id: UUID
    ) -> tuple[models.WebhookDelivery, models.WebhookEndpoint] | None:
        """Recheck and materialize current receiver settings immediately before POST."""
        from preloop.services.event_webhooks.events import build_envelope

        delivery = (
            db.query(models.WebhookDelivery)
            .populate_existing()
            .filter(models.WebhookDelivery.id == delivery_id)
            .first()
        )
        if delivery is None or delivery.status != "pending":
            return None
        endpoint = (
            db.query(models.WebhookEndpoint)
            .populate_existing()
            .filter(models.WebhookEndpoint.id == delivery.endpoint_id)
            .first()
        )
        data = (
            self.callback_payload(
                db,
                endpoint=endpoint,
                account_id=delivery.account_id,
                event_type=delivery.event_type,
                subject_id=delivery.subject_id,
            )
            if endpoint is not None
            else None
        )
        reason = (
            blocked_target_reason(endpoint.url)
            if data is not None and endpoint is not None
            else None
        )
        if data is not None and reason == "unresolvable":
            raise RuntimeError("Restricted CI callback target resolution failed")
        if data is None or endpoint is None or reason:
            delivery.status = "dead"
            delivery.claimed_at = None
            delivery.last_error = "Restricted CI callback authorization denied"
            db.commit()
            return None
        delivery.payload = build_envelope(
            event_id=delivery.event_id,
            event_type=delivery.event_type,
            account_id=delivery.account_id,
            data=data,
            occurred_at=delivery.occurred_at,
        )
        db.commit()
        db.refresh(delivery)
        db.refresh(endpoint)
        return delivery, endpoint

    def record_preparation_failure(self, db: Session, *, delivery_id: UUID) -> None:
        """Account for transient preparation failures without reopening terminal rows."""
        from preloop.services.event_webhooks import outbox

        delivery = (
            db.query(models.WebhookDelivery)
            .populate_existing()
            .filter(models.WebhookDelivery.id == delivery_id)
            .with_for_update()
            .first()
        )
        if delivery is None or delivery.status != "pending":
            return
        endpoint = (
            db.query(models.WebhookEndpoint)
            .populate_existing()
            .filter(models.WebhookEndpoint.id == delivery.endpoint_id)
            .with_for_update()
            .first()
        )
        if endpoint is None or not is_ci_endpoint(endpoint):
            return
        moment = outbox._utcnow()
        if not outbox.endpoint_is_deliverable(endpoint, moment):
            delivery.claimed_at = None
            probe_at = outbox.circuit_probe_at(endpoint)
            if probe_at is not None:
                delivery.next_attempt_at = probe_at
        else:
            outbox.record_attempt(
                db,
                delivery=delivery,
                endpoint=endpoint,
                success=False,
                error="Restricted CI callback preparation failed",
                now=moment,
            )
        db.commit()


crud_ci_subscription = CRUDCiSubscription()
