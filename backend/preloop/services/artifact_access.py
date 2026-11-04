"""Internal artifact read policy seam. No policy means existing core behavior.

Readers bind only authenticated identity, never request filters. The policy is
resolved afresh for every CRUD read; SQL predicates run before limits/facets.
The extension owns assignment and audit, core owns predicate semantics.
Resolvers return None for an unassigned subject, [] for deny-all, or a nonempty
list of intersecting scopes. Missing or malformed assigned scopes deny all.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from sqlalchemy import String, and_, false, or_, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.base import get_plugin_manager

ARTIFACT_READ_POLICY_VERSION = 1
_SUBJECT_KEY = "artifact_reader"


def bind_reader(db: Session, *, account_id: Any, **identity: Any) -> None:
    """Bind trusted identity to a request-local database session."""
    db.info[_SUBJECT_KEY] = {
        "account_id": str(account_id),
        **{key: str(value) for key, value in identity.items() if value is not None},
    }


def bind_user(db: Session, user: Any) -> None:
    """Preserve the validated API-key identity carried by REST authentication."""
    from preloop.services.subject_governance import build_subject_context_from_api_key

    key = getattr(user, "_auth_api_key", None)
    context = build_subject_context_from_api_key(key) if key is not None else {}
    bind_reader(db, account_id=user.account_id, user_id=user.id, **context)


def bind_gateway(db: Session, auth: Any) -> None:
    """Bind validated gateway identity before a deposit reads existing bytes."""
    from preloop.services.model_gateway_auth import resolve_managed_agent_id_for_context
    from preloop.services.subject_governance import build_subject_context_from_api_key

    context = (
        build_subject_context_from_api_key(auth.api_key)
        if auth.api_key is not None
        else {}
    )
    context["managed_agent_id"] = resolve_managed_agent_id_for_context(db, auth)
    bind_reader(db, account_id=auth.account_id, user_id=auth.user.id, **context)


def _policy(db: Session, account_id: Any) -> tuple[Any, dict]:
    subject = db.info.get(_SUBJECT_KEY, {})
    policy = get_plugin_manager().get_service("artifact_read_policy")
    if policy is not None and subject and subject["account_id"] != str(account_id):
        raise ValueError("Artifact reader account mismatch")
    return policy, subject


def predicate(scope: Any) -> Any:
    """Compile an allow-list; malformed or empty scopes deny everything.

    Keys intersect, values within a key union, and kinds intersect labels.
    Only scalar string label values are supported; no wildcard expressions.
    """
    from preloop.models.crud.runtime_session_artifact import validate_labels
    from preloop.services.artifact_media import ARTIFACT_KINDS

    if not isinstance(scope, dict) or set(scope) != {"labels", "kinds"}:
        return false()
    labels, kinds = scope["labels"], scope["kinds"]
    if (
        not isinstance(labels, dict)
        or not labels
        or len(labels) > 32
        or not isinstance(kinds, list)
        or not kinds
        or any(
            not isinstance(kind, str) or kind not in ARTIFACT_KINDS for kind in kinds
        )
    ):
        return false()
    table = models.RuntimeSessionArtifact
    conditions = [table.kind.in_(kinds)]
    for key, values in labels.items():
        if not isinstance(values, list) or not values or len(values) > 100:
            return false()
        terms = []
        for value in values:
            if not isinstance(value, str) or not value:
                return false()
            try:
                validate_labels({key: value})
            except (ValueError, TypeError):
                return false()
            terms.append(table.labels.contains({key: value}))
        conditions.append(or_(*terms))
    return and_(*conditions)


def read_conditions(db: Session, account_id: Any) -> list[Any]:
    """Restrictions are additive to every existing account/ownership check."""
    policy, subject = _policy(db, account_id)
    if policy is None:
        return []
    scopes = policy.resolve(db, account_id=account_id, subject=subject)
    if scopes is None:
        return []
    if not isinstance(scopes, list) or not scopes:
        return [false()]
    return [predicate(scope) for scope in scopes]


def audited_read(func: Callable) -> Callable:
    """Send each returned artifact to the policy audit sink before disclosure."""

    @wraps(func)
    def read(db: Session, *args: Any, **kwargs: Any) -> Any:
        result = func(db, *args, **kwargs)
        policy, subject = _policy(db, kwargs["account_id"])
        if policy is not None:
            rows = result if isinstance(result, list) else [result]
            artifacts = [row[0] if isinstance(row, tuple) else row for row in rows]
            artifacts = [
                row
                for row in artifacts
                if isinstance(row, models.RuntimeSessionArtifact)
            ]
            if artifacts:
                policy.audit(
                    db,
                    account_id=kwargs["account_id"],
                    subject=subject,
                    artifacts=artifacts,
                )
        return result

    return read


def search_document_conditions(db: Session, account_id: Any) -> list[Any]:
    """Apply the same predicate to artifact text in the session-search corpus."""
    conditions = read_conditions(db, account_id)
    if not conditions:
        return []
    document = models.SessionSearchDocument
    artifact = models.RuntimeSessionArtifact
    allowed = select(artifact.id.cast(String)).where(
        artifact.account_id == account_id,
        *conditions,
    )
    return [(document.source_kind != "artifact") | document.source_id.in_(allowed)]


def audited_search(func: Callable) -> Callable:
    """Audit artifact sources whose text or metadata was read through search."""

    @wraps(func)
    def read(self: Any, db: Session, *args: Any, **kwargs: Any) -> Any:
        result = func(self, db, *args, **kwargs)
        account_id = kwargs["account_id"]
        policy, subject = _policy(db, account_id)
        if policy is None or not policy.resolve(
            db, account_id=account_id, subject=subject
        ):
            return result
        rows = result[0] if isinstance(result, tuple) else result
        ids = set()
        for row in rows:
            for hit in getattr(row, "snippets", [row]):
                if getattr(hit, "source_kind", None) == "artifact":
                    ids.add(hit.source_id)
        if ids:
            from preloop.models.crud.runtime_session_artifact import metadata_for_audit

            policy.audit(
                db,
                account_id=account_id,
                subject=subject,
                artifacts=metadata_for_audit(
                    db, account_id=account_id, artifact_ids=ids
                ),
            )
        return result

    return read
