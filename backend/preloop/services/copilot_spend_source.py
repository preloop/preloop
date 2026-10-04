"""Mapped Copilot premium-request spend as a source for spend outliers (#1061).

The Copilot import (:mod:`preloop.services.copilot_usage_import`) stores one
``provider_billing_snapshot`` row per day, GitHub login and model with the
``netAmount`` GitHub billed. The outlier rules in
:mod:`preloop.services.spend_outliers` evaluate Preloop users. This module is
the adapter between the two:

* ``copilot_user_mapping`` rows, written by an operator through the API in
  :mod:`preloop.api.endpoints.copilot_usage`, say which GitHub login is which
  Preloop user. Nothing is inferred from usernames, email addresses, seat
  lists or OAuth identities.
* :func:`copilot_imported_spend` is the source callable registered with
  :func:`preloop.services.spend_outliers.register_imported_spend_source`. It
  reads stored rows only (no GitHub call), keeps per-user daily
  premium-request rows in USD whose login is mapped, nets the amounts per
  user, day and model, and returns the positive nets labelled ``copilot``.
* :func:`spend_coverage` says how much of the stored spend those rows cover
  and why the rest was left out, as counts per reason. Unknown is reported as
  unknown, never as zero.

Everything excluded here (seat rows, seat summaries, usage metrics, the
organization total stored when GitHub refused per-user answers, the
unattributed residual, unmapped logins, rows without an amount) stays in the
Cost page's Copilot summary. Nothing here writes ``api_usage``, budgets,
execution costs or issue rollups.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_copilot_import_connection,
    crud_copilot_usage,
    crud_copilot_user_mapping,
    crud_spend_outlier_finding,
    crud_user,
)
from preloop.models.crud.copilot_import import (
    LINE_ITEM_PREMIUM_REQUEST,
    canonical_github_login,
)
from preloop.services.spend_outliers import (
    ImportedSpendRow,
    register_imported_spend_source,
)

logger = logging.getLogger(__name__)

#: ``source`` label on every row this adapter returns.
COPILOT_SPEND_SOURCE = "copilot"
#: The only currency the outlier rules understand.
SUPPORTED_CURRENCY = "USD"
#: Daily granularity marker written by the import.
DAILY_GRANULARITY = "1d"

#: Reasons a stored premium-request row is not an outlier sample.
REASON_UNMAPPED = "unmapped"
REASON_UNKNOWN_AMOUNT = "unknown_amount"
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"
REASON_NONFINITE = "nonfinite_amount"
REASON_AGGREGATE_ONLY = "aggregate_only"
REASON_UNATTRIBUTED = "unattributed"
REASON_NOT_DAILY = "not_daily"
EXCLUSION_REASONS: Tuple[str, ...] = (
    REASON_UNMAPPED,
    REASON_UNKNOWN_AMOUNT,
    REASON_UNSUPPORTED_CURRENCY,
    REASON_NONFINITE,
    REASON_AGGREGATE_ONLY,
    REASON_UNATTRIBUTED,
    REASON_NOT_DAILY,
)


class CopilotMappingError(ValueError):
    """A mapping write was refused.

    Attributes:
        reason: ``no_connection`` (connect an organization first),
            ``invalid_login`` or ``invalid_user`` (not an active user of this
            account; the message never says whether the id exists elsewhere).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def _active_connection(
    db: Session, account_id: UUID | str
) -> models.CopilotImportConnection:
    connection = crud_copilot_import_connection.get_active_for_account(
        db, account_id=account_id
    )
    if connection is None:
        raise CopilotMappingError(
            "Connect an active Copilot organization before mapping logins.",
            reason="no_connection",
        )
    return connection


def list_user_mappings(db: Session, *, account_id: UUID | str) -> Dict[str, Any]:
    """Mappings for the account's current organization.

    Returns:
        ``{"organization", "items"}``; ``organization`` is None and ``items``
        empty without a connection. Paused connections still list their
        mappings so an operator can see what will apply once resumed.
    """
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account_id
    )
    if connection is None:
        return {"organization": None, "items": []}
    mappings = crud_copilot_user_mapping.list_for_connection(db, connection=connection)
    names = crud_spend_outlier_finding.user_display_names(
        db,
        account_id=connection.account_id,
        user_ids=[mapping.user_id for mapping in mappings],
    )
    return {
        "organization": connection.organization,
        "items": [
            mapping_payload(mapping, names.get(mapping.user_id)) for mapping in mappings
        ],
    }


def mapping_payload(
    mapping: models.CopilotUserMapping, user_name: Optional[str]
) -> Dict[str, Any]:
    """Serialize one mapping. ``user_name`` must be resolved in-account."""
    return {
        "github_login": mapping.github_login,
        "user_id": mapping.user_id,
        "user_name": user_name,
        "organization": mapping.organization,
        "created_at": mapping.created_at,
        "updated_at": mapping.updated_at,
    }


def upsert_user_mapping(
    db: Session, *, account_id: UUID | str, github_login: str, user_id: UUID
) -> Tuple[models.CopilotUserMapping, Optional[str]]:
    """Map a login of the current organization to an active user of the account.

    Args:
        db: Database session.
        account_id: Caller's account.
        github_login: Login in any spelling; stored canonical.
        user_id: Target user.

    Returns:
        The stored mapping and the target's display name.

    Raises:
        CopilotMappingError: No active connection, an empty login, or a
            target that is not an active user of this account. The last
            message is the same whether the id is unknown, belongs to
            another account or is deactivated.
    """
    connection = _active_connection(db, account_id)
    try:
        login = canonical_github_login(github_login)
    except ValueError as exc:
        raise CopilotMappingError(str(exc), reason="invalid_login") from exc
    user = crud_user.get(db, id=user_id)
    if (
        user is None
        or str(user.account_id) != str(connection.account_id)
        or not user.is_active
    ):
        raise CopilotMappingError(
            "No active user with that id in this account.", reason="invalid_user"
        )
    mapping = crud_copilot_user_mapping.upsert(
        db, connection=connection, github_login=login, user_id=user.id
    )
    names = crud_spend_outlier_finding.user_display_names(
        db, account_id=connection.account_id, user_ids=[user.id]
    )
    return mapping, names.get(user.id)


def delete_user_mapping(
    db: Session, *, account_id: UUID | str, github_login: str
) -> bool:
    """Remove one login's mapping. Returns False when there was none.

    Deleting is allowed on a paused connection too, so an operator can clean
    up before resuming.

    Raises:
        CopilotMappingError: No connection at all, or an empty login.
    """
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account_id
    )
    if connection is None:
        raise CopilotMappingError(
            "No Copilot connection for this account.", reason="no_connection"
        )
    try:
        login = canonical_github_login(github_login)
    except ValueError as exc:
        raise CopilotMappingError(str(exc), reason="invalid_login") from exc
    return (
        crud_copilot_user_mapping.delete_for_login(
            db, connection=connection, github_login=login
        )
        > 0
    )


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _row_day(bucket_start: datetime) -> date:
    moment = bucket_start
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


@dataclass
class _Classified:
    """Where each stored premium-request row ended up."""

    #: Signed net per (user id, UTC day, model) over the mapped rows.
    net: Dict[Tuple[UUID, date, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    reasons: Counter[str] = field(default_factory=Counter)
    mapped_rows: int = 0
    known_zero_rows: int = 0
    unmapped_logins: Set[str] = field(default_factory=set)


def _classify(
    rows: Iterable[models.ProviderBillingSnapshot], mapping: Dict[str, UUID]
) -> _Classified:
    out = _Classified()
    for row in rows:
        if (row.granularity or DAILY_GRANULARITY) != DAILY_GRANULARITY:
            out.reasons[REASON_NOT_DAILY] += 1
            continue
        if not row.user_login:
            if (row.raw or {}).get("unattributed"):
                out.reasons[REASON_UNATTRIBUTED] += 1
            else:
                out.reasons[REASON_AGGREGATE_ONLY] += 1
            continue
        try:
            login = canonical_github_login(row.user_login)
        except ValueError:
            # A blank login is nobody's; the import never writes one, but a
            # row that carries one is unmapped rather than an exception.
            out.reasons[REASON_UNMAPPED] += 1
            continue
        user_id = mapping.get(login)
        if user_id is None:
            out.reasons[REASON_UNMAPPED] += 1
            out.unmapped_logins.add(login)
            continue
        if row.cost_amount is None:
            out.reasons[REASON_UNKNOWN_AMOUNT] += 1
            continue
        if (row.currency or SUPPORTED_CURRENCY).upper() != SUPPORTED_CURRENCY:
            out.reasons[REASON_UNSUPPORTED_CURRENCY] += 1
            continue
        try:
            amount = float(row.cost_amount)
        except (TypeError, ValueError):
            out.reasons[REASON_NONFINITE] += 1
            continue
        if not math.isfinite(amount):
            out.reasons[REASON_NONFINITE] += 1
            continue
        out.mapped_rows += 1
        if amount == 0:
            out.known_zero_rows += 1
        key = (user_id, _row_day(row.bucket_start), row.model or "unknown")
        out.net[key] += amount
    return out


def _premium_rows(
    db: Session,
    connection: models.CopilotImportConnection,
    start_day: date,
    end_day: date,
) -> List[models.ProviderBillingSnapshot]:
    """Stored premium-request rows of the connected organization, ``[start, end]``."""
    if end_day < start_day:
        return []
    return crud_copilot_usage.list_rows(
        db,
        account_id=connection.account_id,
        line_item=LINE_ITEM_PREMIUM_REQUEST,
        start=_day_start(start_day),
        end=_day_start(end_day + timedelta(days=1)),
        organization=connection.organization,
    )


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


def copilot_imported_spend(
    db: Session, account_id: UUID, start_day: date, end_day: date
) -> List[ImportedSpendRow]:
    """Mapped premium-request spend per user, day and model for ``[start, end]``.

    The registered imported-spend source. It reads only what the import
    already stored; GitHub is never called from here. Without an active
    connection, or with no valid mapping, it returns nothing and the gateway
    rules run as before.

    Rows kept: provider ``copilot``, ``usage_source='imported'``, line item
    ``premium_request``, daily granularity, for the connection's current
    organization, with a mapped login, a known amount and USD. Signed amounts
    are netted per user, UTC day and model (so a credit lowers that day, and
    two logins mapped to one user add up); only positive nets are returned.
    A zero net is a known zero and is simply absent.

    Args:
        db: Database session.
        account_id: Account to read.
        start_day: First UTC day, inclusive.
        end_day: Last UTC day, inclusive.

    Returns:
        Rows with ``source='copilot'`` and the stored net billed amounts.
    """
    connection = crud_copilot_import_connection.get_active_for_account(
        db, account_id=account_id
    )
    if connection is None:
        return []
    mapping = crud_copilot_user_mapping.resolve_user_ids(db, connection=connection)
    if not mapping:
        return []
    classified = _classify(_premium_rows(db, connection, start_day, end_day), mapping)
    return [
        ImportedSpendRow(
            user_id=user_id,
            day=day,
            model=model,
            cost_usd=net,
            source=COPILOT_SPEND_SOURCE,
        )
        for (user_id, day, model), net in sorted(
            classified.net.items(),
            key=lambda item: (str(item[0][0]), item[0][1], item[0][2]),
        )
        if net > 0
    ]


def copilot_replay_account_ids(db: Session) -> List[UUID]:
    """Accounts whose recent days the daily pass replays: active connections."""
    return crud_copilot_import_connection.list_active_account_ids(db)


def register_copilot_spend_source() -> None:
    """Register :func:`copilot_imported_spend` with the outlier evaluator.

    Idempotent: the evaluator keeps one entry per callable, so calling this
    at every task start is harmless. Nothing here imports the HTTP layer or
    needs a prior sync, so a fresh worker process can call it before its
    first evaluation.
    """
    register_imported_spend_source(
        copilot_imported_spend, replay_accounts=copilot_replay_account_ids
    )


# ---------------------------------------------------------------------------
# Coverage diagnostics
# ---------------------------------------------------------------------------


def spend_coverage(
    db: Session, *, account_id: UUID | str, start_day: date, end_day: date
) -> Dict[str, Any]:
    """How much stored premium-request spend the outlier rules can see.

    Counts rows per outcome for the account's current organization over
    ``[start_day, end_day]``. Only logins and counts are reported: no tokens,
    no raw payloads.

    Returns:
        ``organization`` (None without a connection), ``connection_active``,
        ``period_start``/``period_end`` (UTC days), ``mapped_rows``,
        ``mapped_net_amount`` (the sum of the positive user/day/model nets,
        which is exactly what :func:`copilot_imported_spend` emits; None when
        no row was mapped, because unknown is not zero), ``credited_net_amount``
        (the user/day/model nets that came out at or below zero and therefore
        reach no rule; None when there were none), ``known_zero_rows``,
        ``excluded`` (one count per reason in :data:`EXCLUSION_REASONS`),
        ``unmapped_logins`` (sorted, canonical) and ``mapped_logins``.
    """
    excluded = {reason: 0 for reason in EXCLUSION_REASONS}
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account_id
    )
    base: Dict[str, Any] = {
        "organization": connection.organization if connection else None,
        "connection_active": bool(connection and connection.is_active),
        "period_start": start_day,
        "period_end": end_day,
        "mapped_rows": 0,
        "mapped_net_amount": None,
        "credited_net_amount": None,
        "known_zero_rows": 0,
        "excluded": excluded,
        "unmapped_logins": [],
        "mapped_logins": [],
    }
    if connection is None:
        return base
    mapping = crud_copilot_user_mapping.resolve_user_ids(db, connection=connection)
    classified = _classify(_premium_rows(db, connection, start_day, end_day), mapping)
    excluded.update(classified.reasons)
    positive = [net for net in classified.net.values() if net > 0]
    credited = [net for net in classified.net.values() if net <= 0]
    base.update(
        {
            "mapped_rows": classified.mapped_rows,
            "mapped_net_amount": (
                round(sum(positive), 4) if classified.mapped_rows else None
            ),
            "credited_net_amount": round(sum(credited), 4) if credited else None,
            "known_zero_rows": classified.known_zero_rows,
            "unmapped_logins": sorted(classified.unmapped_logins),
            "mapped_logins": sorted(mapping),
        }
    )
    return base


__all__: Sequence[str] = (
    "COPILOT_SPEND_SOURCE",
    "EXCLUSION_REASONS",
    "CopilotMappingError",
    "copilot_imported_spend",
    "copilot_replay_account_ids",
    "delete_user_mapping",
    "list_user_mappings",
    "mapping_payload",
    "register_copilot_spend_source",
    "spend_coverage",
    "upsert_user_mapping",
)
