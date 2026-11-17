"""Bounded maintenance for execution output credential masking."""

import base64
from collections import Counter
from collections.abc import Iterator
import hashlib
import json
import re
from typing import Any
from uuid import UUID

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.utils.execution_credentials import CredentialMasker

_RUNTIME_KEY = re.compile(r"flow_[A-Za-z0-9_-]{43}")
_SIGNED = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_BASE64_KEY = re.compile(r"Zmxvd1[A-Za-z0-9+/_-]{58}")
_EXECUTION_FIELDS = (
    "execution_logs",
    "cli_session",
    "mcp_usage_logs",
    "model_output_summary",
    "actions_taken_summary",
    "result",
    "error_message",
    "resolved_input_prompt",
)


def discover_values(
    value: Any, *, key_hashes: set[str], execution_id: UUID
) -> set[str]:
    """Confirm stored candidates against key hashes or signed execution claims.

    The API key table stores hashes. Matching a candidate's full digest is an
    exact-value check; token-shaped prose alone never authorizes replacement.
    Expired capabilities are checked with expiry disabled, keeping signature,
    audience and execution binding validation.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    candidates = set(_RUNTIME_KEY.findall(text))
    for encoded in _BASE64_KEY.findall(text):
        try:
            candidates.add(base64.urlsafe_b64decode(encoded).decode())
        except (ValueError, UnicodeError):
            continue
    found = {
        token
        for token in candidates
        if hashlib.sha256(token.encode()).hexdigest() in key_hashes
    }
    for token in _SIGNED.findall(text):
        try:
            claims = jwt.decode(
                token,
                settings.security.secret_key,
                algorithms=["HS256"],
                audience=[
                    "flow-artifact",
                    "execution-publication-credential",
                    "preloop:host-exec-gateway",
                ],
                options={
                    "verify_exp": False,
                    "require": ["aud", "execution_id", "exp"],
                },
            )
        except jwt.PyJWTError:
            continue
        if str(claims.get("execution_id")) == str(execution_id):
            found.add(token)
    return found


def _output_rows(
    db: Session, execution: models.FlowExecution
) -> Iterator[tuple[Any, tuple[str, ...]]]:
    """Yield bounded batches from execution, timeline and derived search stores."""
    yield execution, _EXECUTION_FIELDS
    session_ids = select(models.RuntimeSession.id).where(
        models.RuntimeSession.account_id == execution.flow.account_id,
        models.RuntimeSession.session_source_type == "flow_execution",
        models.RuntimeSession.session_source_id == str(execution.id),
    )
    sources = (
        (
            models.RuntimeSession,
            models.RuntimeSession.id.in_(session_ids),
            ("title", "summary"),
        ),
        (
            models.FlowExecutionLog,
            models.FlowExecutionLog.execution_id == execution.id,
            ("message", "metadata_"),
        ),
        (
            models.RuntimeSessionActivity,
            (models.RuntimeSessionActivity.flow_execution_id == execution.id)
            | models.RuntimeSessionActivity.runtime_session_id.in_(session_ids),
            ("summary", "metadata_", "server_name", "tool_name"),
        ),
        (
            models.SessionSearchDocument,
            models.SessionSearchDocument.runtime_session_id.in_(session_ids),
            ("content", "meta_data"),
        ),
        (
            models.ApiUsage,
            models.ApiUsage.flow_execution_id == execution.id,
            ("meta_data",),
        ),
    )
    for model, predicate, fields in sources:
        after = None
        while True:
            query = select(model).where(predicate).order_by(model.id).limit(200)
            if after is not None:
                query = query.where(model.id > after)
            rows = db.execute(query).scalars().all()
            if not rows:
                break
            for row in rows:
                yield row, fields
            after = rows[-1].id


def redact_stored_execution_credentials(
    db: Session,
    *,
    account_id: UUID,
    execution_ids: list[UUID],
    apply: bool = False,
) -> dict[str, int]:
    """Count or replace exact credential matches, committing one execution at a time.

    Args:
        db: Admin-owned session; never an HTTP request session.
        account_id: Required tenant boundary.
        execution_ids: Explicit executions; empty selects the account's runs.
        apply: False performs no writes or commits.

    Returns:
        Counts only, with no credential material or captured output.
    """
    counts: Counter[str] = Counter()
    after = None
    while True:
        query = (
            select(models.FlowExecution)
            .join(models.Flow)
            .where(models.Flow.account_id == account_id)
        )
        if execution_ids:
            query = query.where(models.FlowExecution.id.in_(execution_ids))
        if after is not None:
            query = query.where(models.FlowExecution.id > after)
        executions = (
            db.execute(query.order_by(models.FlowExecution.id).limit(50))
            .scalars()
            .all()
        )
        if not executions:
            break
        for execution in executions:
            counts["executions_scanned"] += 1
            key_hashes = {
                value
                for value in db.execute(
                    select(models.ApiKey.key_hash).where(
                        models.ApiKey.account_id == account_id,
                        models.ApiKey.context_data["flow_execution_id"].as_string()
                        == str(execution.id),
                        models.ApiKey.key_hash.is_not(None),
                    )
                )
                .scalars()
                .all()
                if value is not None
            }
            values: set[str] = set()
            for row, fields in _output_rows(db, execution):
                for field in fields:
                    values.update(
                        discover_values(
                            getattr(row, field),
                            key_hashes=key_hashes,
                            execution_id=UUID(str(execution.id)),
                        )
                    )
            masker = CredentialMasker(
                [
                    *(execution.credential_fingerprints or []),
                    *CredentialMasker.from_values(values).fingerprints,
                ]
            )
            changed_execution = False
            for row, fields in _output_rows(db, execution):
                changed = False
                for field in fields:
                    before = getattr(row, field)
                    after_value = masker.mask(before)
                    if before != after_value:
                        changed = True
                        counts["fields_changed"] += 1
                        if apply:
                            setattr(row, field, after_value)
                if changed:
                    changed_execution = True
                    counts[row.__tablename__ + "_rows_changed"] += 1
                    if apply and isinstance(row, models.SessionSearchDocument):
                        row.content_hash = hashlib.sha256(
                            row.content.encode()
                        ).hexdigest()
                        row.search_vector = None
                        row.embedding = None
                        row.embedding_state = "pending"
                        row.embedded_at = None
            if changed_execution:
                counts["executions_changed"] += 1
            if apply:
                execution.credential_fingerprints = masker.fingerprints  # type: ignore[assignment]
                db.commit()
        after = executions[-1].id
    return dict(counts)
