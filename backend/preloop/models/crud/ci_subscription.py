"""Principal-owned subscriptions and fresh trusted completion eligibility."""

from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.ci_principal import CiAuthorizationContext, crud_ci_principal
from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.models.webhook_endpoint import (
    DELIVERY_KIND_GITHUB_DISPATCH,
    DELIVERY_KIND_HTTP,
)
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


def is_github_dispatch(endpoint: models.WebhookEndpoint) -> bool:
    """Whether completion for this endpoint is a GitHub repository_dispatch."""
    return getattr(endpoint, "delivery_kind", None) == DELIVERY_KIND_GITHUB_DISPATCH


def github_api_base(tracker_url: str | None) -> str:
    """Return the REST API root for a bound GitHub integration.

    github.com integrations use ``https://api.github.com``; a GitHub
    Enterprise Server integration uses ``<host>/api/v3``.

    Raises:
        ValueError: When the integration URL is not a plain HTTPS origin.
    """
    if tracker_url is None:
        return "https://api.github.com"
    parsed = urlsplit(tracker_url.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Unsupported GitHub integration URL")
    if parsed.hostname in {"github.com", "api.github.com", "www.github.com"}:
        return "https://api.github.com"
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v3"):
        path = f"{path}/api/v3"
    return f"https://{parsed.netloc}{path}"


def github_dispatch_url(binding: CiSubscriptionBinding) -> str:
    """Derive the repository_dispatch URL from the immutable binding only.

    The stored ``url`` column is display only for dispatch endpoints; every
    delivery recomputes the target from the binding, so no update can point
    the dispatch credential at another host or repository.

    Raises:
        ValueError: When the binding is not a GitHub repository.
    """
    if binding.tracker_type != "github":
        raise ValueError("GitHub dispatch requires a GitHub repository binding")
    parts = binding.repository_slug.split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Unsupported GitHub repository slug")
    owner, repo = (quote(part, safe="") for part in parts)
    return f"{github_api_base(binding.tracker_url)}/repos/{owner}/{repo}/dispatches"


def delivery_target(endpoint: models.WebhookEndpoint) -> str:
    """The URL a delivery for this endpoint is sent to."""
    if is_github_dispatch(endpoint):
        return github_dispatch_url(_binding(endpoint))
    return endpoint.url


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
        or (
            getattr(endpoint, "delivery_kind", None) not in (None, DELIVERY_KIND_HTTP)
            and (
                not is_github_dispatch(endpoint)
                or binding.tracker_type != "github"
                or not endpoint.dispatch_event_type
                or not endpoint.dispatch_token_encrypted
            )
        )
    ):
        raise ValueError("Invalid CI subscription attribution")
    return binding


def _result_digest(result: Any) -> str | None:
    """Digest of the result projection served to the CI principal, or None."""
    if result is None:
        return None
    from preloop.services.ci_execution import public_ci_result
    from preloop.services.execution_tool_evidence import digest

    return digest(public_ci_result(result))


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
        return self._persist(
            db, context=fresh, payload=payload, max_endpoints=max_endpoints
        )

    def _persist(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        payload: CiSubscriptionCreate,
        max_endpoints: int = 20,
        commit: bool = True,
        dispatch: tuple[str, str] | None = None,
    ) -> tuple[models.WebhookEndpoint, str]:
        """Persist after explicit machine or human authority has been checked.

        Args:
            dispatch: ``(event_type, token)`` for a GitHub repository_dispatch
                endpoint. Only the human administration path passes it.
        """
        fresh = context
        payload = CiSubscriptionCreate.model_validate(payload)
        if dispatch is not None:
            payload = CiSubscriptionCreate(
                url=github_dispatch_url(_snapshot(fresh)),
                description=payload.description,
                active=payload.active,
            )
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
        if dispatch is not None:
            event_type, token = dispatch
            endpoint.delivery_kind = DELIVERY_KIND_GITHUB_DISPATCH
            endpoint.dispatch_event_type = event_type
            endpoint.dispatch_token_encrypted = encrypt_value(token)
        db.add(endpoint)
        if commit:
            db.commit()
            db.refresh(endpoint)
        else:
            db.flush()
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
        if (
            is_github_dispatch(endpoint)
            and "url" in fields
            and fields["url"] != endpoint.url
        ):
            raise ValueError("A GitHub dispatch target follows the bound repository")
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

    def replace_dispatch_token(
        self, db: Session, *, endpoint: models.WebhookEndpoint, token: str
    ) -> models.WebhookEndpoint:
        """Store a new dispatch credential and close the circuit, uncommitted.

        Raises:
            LookupError: When the endpoint is not a GitHub dispatch endpoint.
        """
        if not is_github_dispatch(endpoint):
            raise LookupError("Webhook endpoint not found")
        endpoint.dispatch_token_encrypted = encrypt_value(token)
        endpoint.consecutive_failures = 0
        endpoint.circuit_opened_at = None
        db.flush()
        return endpoint

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
            if execution is None or execution.status not in (
                CRUDFlowExecution.TERMINAL_EXECUTION_STATUSES
            ):
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
                # The reviewed snapshot: bound repository, PR and both
                # revisions as accepted from the provider, plus the digest of
                # the result a reader of ``.../result`` will receive.
                "tracker_type": review.tracker_type,
                "repository_slug": review.repository_slug,
                "base_branch": review.base_branch,
                "base_sha": review.base_sha,
                "result_sha256": _result_digest(execution.result),
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
        try:
            target = (
                delivery_target(endpoint)
                if data is not None and endpoint is not None
                else None
            )
        except (ValidationError, ValueError, TypeError):
            target, data = None, None
        reason = blocked_target_reason(target) if target is not None else None
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
