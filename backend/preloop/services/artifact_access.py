"""Internal artifact read policy seam. No policy means existing core behavior.

Readers bind only authenticated identity, never request filters. The policy is
resolved afresh for every CRUD read; SQL predicates run before limits/facets.
The extension owns assignment and audit, core owns predicate semantics.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from sqlalchemy import and_, false, or_
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


def _policy(db: Session, account_id: Any) -> tuple[Any, dict]:
    subject = db.info.get(_SUBJECT_KEY, {})
    if subject and subject["account_id"] != str(account_id):
        raise ValueError("Artifact reader account mismatch")
    return get_plugin_manager().get_service("artifact_read_policy"), subject


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
    if not isinstance(scopes, list):
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
