"""CRUD for OTLP telemetry ingest state and request matching (issue #1412).

Holds every query the OTLP receiver needs: dedup key claims, cumulative
series state, the lookups that match a client ``api_request`` event to a
gateway usage row, and the supersede step that keeps one usage row per
request when a gateway row lands after the telemetry did.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import String, and_, cast, delete, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models.api_usage import ApiUsage
from ..models.gateway_subject import GatewaySubject
from ..models.user import User
from ..models.runtime_session import RuntimeSession
from ..models.telemetry_ingest import TelemetryIngestDedup, TelemetryMetricSeries

logger = logging.getLogger(__name__)

#: Dedup keys are kept a week: exporters retry within minutes, and a week
#: covers a gateway relay that was down over a weekend.
DEDUP_RETENTION = timedelta(days=7)
#: Cumulative series state expires after a day without data points.
SERIES_TTL = timedelta(hours=24)

#: Time window for the signature match (see ``find_gateway_usage_by_signature``).
SIGNATURE_WINDOW = timedelta(seconds=60)
#: Clients whose gateway rows can have OTLP counterparts.
CLAUDE_CLIENTS = frozenset({"claude_code", "claude_desktop"})
APPS_GATEWAY_SOURCE = "claude_apps_gateway"

IMPORTED_ACTION = "imported_usage"
GATEWAY_ACTION = "model_gateway"
OTLP_SOURCE = "otlp"


class CRUDTelemetryIngest:
    """Queries for the OTLP receiver. Stateless; one shared instance."""

    # --- Dedup ------------------------------------------------------------

    @staticmethod
    def claim(
        db: Session, *, account_id: Any, dedup_key: str, kind: str, now: datetime
    ) -> bool:
        """Claim a dedup key. Returns False when it was already claimed.

        ``INSERT ... ON CONFLICT DO NOTHING`` on the unique index, so two
        concurrent deliveries of the same batch cannot both claim it.
        """
        statement = (
            pg_insert(TelemetryIngestDedup)
            .values(account_id=account_id, dedup_key=dedup_key, kind=kind, seen_at=now)
            .on_conflict_do_nothing(index_elements=["account_id", "dedup_key"])
            .returning(TelemetryIngestDedup.id)
        )
        return db.execute(statement).first() is not None

    @staticmethod
    def claim_many(
        db: Session, *, account_id: Any, keys: list[tuple[str, str]], now: datetime
    ) -> set[str]:
        """Claim many ``(dedup_key, kind)`` pairs; return the newly claimed keys.

        One multi-row ``INSERT ... ON CONFLICT DO NOTHING`` per 1,000 keys, so
        a 10,000-record batch costs ten statements, not ten thousand.
        """
        unique = list(dict.fromkeys(keys))
        claimed: set[str] = set()
        for start in range(0, len(unique), 1000):
            chunk = unique[start : start + 1000]
            statement = (
                pg_insert(TelemetryIngestDedup)
                .values(
                    [
                        {
                            "id": uuid.uuid4(),
                            "account_id": account_id,
                            "dedup_key": key,
                            "kind": kind,
                            "seen_at": now,
                        }
                        for key, kind in chunk
                    ]
                )
                .on_conflict_do_nothing(index_elements=["account_id", "dedup_key"])
                .returning(TelemetryIngestDedup.dedup_key)
            )
            claimed.update(db.execute(statement).scalars())
        return claimed

    @staticmethod
    def is_claimed(db: Session, *, account_id: Any, dedup_key: str) -> bool:
        """Return whether a dedup key exists for the account."""
        return bool(
            db.execute(
                select(
                    exists().where(
                        TelemetryIngestDedup.account_id == account_id,
                        TelemetryIngestDedup.dedup_key == dedup_key,
                    )
                )
            ).scalar()
        )

    @staticmethod
    def prune(db: Session, *, now: datetime) -> None:
        """Drop expired dedup keys and series state (retention, scope 9)."""
        db.execute(
            delete(TelemetryIngestDedup).where(
                TelemetryIngestDedup.seen_at < now - DEDUP_RETENTION
            )
        )
        db.execute(
            delete(TelemetryMetricSeries).where(
                TelemetryMetricSeries.updated_at < now - SERIES_TTL
            )
        )

    # --- Cumulative series ------------------------------------------------

    @staticmethod
    def get_series(
        db: Session, *, account_id: Any, series_key: str, now: datetime
    ) -> Optional[TelemetryMetricSeries]:
        """Return live series state; state older than the TTL reads as absent."""
        series = db.execute(
            select(TelemetryMetricSeries).where(
                TelemetryMetricSeries.account_id == account_id,
                TelemetryMetricSeries.series_key == series_key,
            )
        ).scalar_one_or_none()
        if series is None:
            return None
        if series.updated_at is not None and series.updated_at < now - SERIES_TTL:
            db.delete(series)
            db.flush()
            return None
        return series

    @staticmethod
    def put_series(
        db: Session,
        *,
        account_id: Any,
        series_key: str,
        value: float,
        point_time: datetime,
        start_time: Optional[datetime],
        now: datetime,
    ) -> None:
        """Upsert the last cumulative value of a series."""
        statement = pg_insert(TelemetryMetricSeries).values(
            account_id=account_id,
            series_key=series_key,
            last_value=value,
            last_time=point_time,
            start_time=start_time,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["account_id", "series_key"],
            set_={
                "last_value": statement.excluded.last_value,
                "last_time": statement.excluded.last_time,
                "start_time": statement.excluded.start_time,
                "updated_at": statement.excluded.updated_at,
            },
        )
        db.execute(statement)

    # --- Matching client telemetry to usage rows --------------------------

    @staticmethod
    def find_gateway_usage(
        db: Session,
        *,
        account_id: Any,
        client_request_id: Optional[str],
        request_id: Optional[str],
    ) -> Optional[ApiUsage]:
        """Find the gateway row a client ``api_request`` event describes.

        ``client_request_id`` (``x-client-request-id``, recorded by the
        gateway in ``meta_data``) is tried first because the gateway does not
        relay the upstream ``request-id`` header to the client. A direct
        ``upstream_request_id`` match covers paths where the two agree.
        """
        if client_request_id:
            row = db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.account_id == account_id,
                    ApiUsage.action_type == GATEWAY_ACTION,
                    ApiUsage.meta_data["client_request_id"].astext == client_request_id,
                )
                .order_by(ApiUsage.timestamp.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                return row
        if request_id:
            return db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.account_id == account_id,
                    ApiUsage.upstream_request_id == request_id,
                    ApiUsage.action_type != IMPORTED_ACTION,
                )
                .order_by(ApiUsage.timestamp.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None

    @staticmethod
    def _otlp_request_filter(
        client_request_id: Optional[str], request_id: Optional[str]
    ) -> Optional[Any]:
        clauses = []
        if client_request_id:
            clauses.append(
                ApiUsage.meta_data["otlp_client_request_id"].astext == client_request_id
            )
        if request_id:
            clauses.append(ApiUsage.meta_data["otlp_request_id"].astext == request_id)
        if not clauses:
            return None
        return or_(*clauses)

    def find_otlp_rows(
        self,
        db: Session,
        *,
        account_id: Any,
        client_request_id: Optional[str],
        request_id: Optional[str],
    ) -> list[ApiUsage]:
        """Return OTLP-created rows for a request id pair."""
        match = self._otlp_request_filter(client_request_id, request_id)
        if match is None:
            return []
        return list(
            db.execute(
                select(ApiUsage).where(
                    ApiUsage.account_id == account_id,
                    ApiUsage.action_type == IMPORTED_ACTION,
                    match,
                )
            ).scalars()
        )

    def supersede_for_gateway_row(self, db: Session, gateway_row: ApiUsage) -> int:
        """Remove OTLP rows a just-recorded gateway row now accounts for.

        Called in the gateway's own transaction right after its usage row is
        flushed, so one request never has both rows (scope 5, late gateway
        rows). The telemetry enrichment the OTLP row carried moves onto the
        gateway row. The dedup key stays claimed, so a re-sent export finds
        the gateway row and enriches it instead of recreating the OTLP row.

        Returns:
            Number of OTLP rows removed.
        """
        meta = gateway_row.meta_data if isinstance(gateway_row.meta_data, dict) else {}
        client_request_id = meta.get("client_request_id")
        request_id = gateway_row.upstream_request_id
        if not gateway_row.account_id or not _may_have_telemetry(meta):
            # Only Claude clients export this telemetry; every other gateway
            # request skips the lookup and costs no extra query.
            return 0
        rows = self.find_otlp_rows(
            db,
            account_id=gateway_row.account_id,
            client_request_id=client_request_id
            if isinstance(client_request_id, str)
            else None,
            request_id=request_id,
        )
        if not rows:
            rows = self._otlp_rows_by_signature(db, gateway_row)
        if not rows:
            return 0
        telemetry = None
        for row in rows:
            row_meta = row.meta_data if isinstance(row.meta_data, dict) else {}
            telemetry = telemetry or row_meta.get("telemetry")
            db.delete(row)
        if telemetry and "telemetry" not in meta:
            gateway_row.meta_data = {**meta, "telemetry": telemetry}
            flag_modified(gateway_row, "meta_data")
        db.flush()
        logger.info(
            "Gateway usage %s superseded %d OTLP telemetry row(s)",
            gateway_row.id,
            len(rows),
        )
        return len(rows)

    # --- Signature match (no shared request id) ----------------------------

    @staticmethod
    def lock_session(db: Session, *, account_id: Any, session_id: str) -> None:
        """Serialize ingest work on one session until the transaction ends.

        A logs batch and a metrics batch of the same session may arrive at
        the same time; without this both could pass their "is the session
        covered" checks and commit an aggregate next to per-request rows.
        Callers take locks in sorted order to avoid deadlocks.
        """
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"otlp:{account_id}:{session_id}"},
        )

    @staticmethod
    def _identity_clause(
        account_id: Any, email: Optional[str], external_subject: Optional[str]
    ) -> Any:
        """Gateway row belongs to this person (email or IdP subject)."""
        clauses = []
        if email:
            clauses.append(
                func.lower(ApiUsage.meta_data["gateway_subject_email"].astext)
                == email.lower()
            )
        if external_subject:
            subject_ids = select(cast(GatewaySubject.id, String)).where(
                GatewaySubject.account_id == account_id,
                GatewaySubject.external_subject == external_subject,
            )
            clauses.append(
                ApiUsage.meta_data["gateway_subject_id"].astext.in_(subject_ids)
            )
        return or_(*clauses) if clauses else None

    def find_gateway_usage_by_signature(
        self,
        db: Session,
        *,
        account_id: Any,
        timestamp: datetime,
        model: Optional[str],
        output_tokens: Optional[int],
        email: Optional[str],
        external_subject: Optional[str],
    ) -> Optional[ApiUsage]:
        """Match an event to a gateway row when no request id is shared.

        Observed on the apps gateway harness (gateway 2.1.288): the relayed
        ``api_request`` events carry neither ``request_id`` nor
        ``client_request_id``, and the gateway forwards neither id upstream.
        The event and the gateway row still agree on completion time, model
        and output tokens, and on the person. A row matches when it is in
        the account, within :data:`SIGNATURE_WINDOW`, has the same output
        token count and model, belongs to the same person, and was not
        enriched yet. The closest in time wins. Same person: the gateway
        subject's email or IdP subject matches, or the row names no subject
        and its key owner has the event's email. An event that names nobody
        only matches rows that name nobody.
        """
        if not output_tokens or not model:
            return None
        no_subject = ApiUsage.meta_data["gateway_subject_id"].astext.is_(None)
        identity = self._identity_clause(account_id, email, external_subject)
        if identity is None:
            # The event names nobody: only rows that name nobody either.
            person = no_subject
        elif email:
            # A row without a gateway subject is the key owner's request;
            # it is this person's only when the owner has this email.
            owner_ids = select(User.id).where(
                User.account_id == account_id, func.lower(User.email) == email.lower()
            )
            person = or_(identity, and_(no_subject, ApiUsage.user_id.in_(owner_ids)))
        else:
            person = identity
        candidates = db.execute(
            select(ApiUsage)
            .where(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == GATEWAY_ACTION,
                ApiUsage.timestamp >= timestamp - SIGNATURE_WINDOW,
                ApiUsage.timestamp <= timestamp + SIGNATURE_WINDOW,
                ApiUsage.completion_tokens == output_tokens,
                or_(
                    ApiUsage.meta_data.is_(None),
                    ~ApiUsage.meta_data.has_key("telemetry"),
                ),
                person,
            )
            .limit(20)
        ).scalars()
        matches = [row for row in candidates if _same_model(row.model_alias, model)]
        if not matches:
            return None
        return min(
            matches, key=lambda row: abs((row.timestamp - timestamp).total_seconds())
        )

    @staticmethod
    def _otlp_rows_by_signature(db: Session, gateway_row: ApiUsage) -> list[ApiUsage]:
        """OTLP rows a gateway row accounts for, without a shared request id."""
        meta = gateway_row.meta_data if isinstance(gateway_row.meta_data, dict) else {}
        if not (
            meta.get("client") in CLAUDE_CLIENTS
            or meta.get("gateway_source") == APPS_GATEWAY_SOURCE
        ):
            return []
        if not gateway_row.completion_tokens or not gateway_row.model_alias:
            return []
        when = gateway_row.timestamp
        if when is None:
            return []
        if when.tzinfo is not None:
            when = when.replace(tzinfo=None)
        candidates = list(
            db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.account_id == gateway_row.account_id,
                    ApiUsage.action_type == IMPORTED_ACTION,
                    ApiUsage.timestamp >= when - SIGNATURE_WINDOW,
                    ApiUsage.timestamp <= when + SIGNATURE_WINDOW,
                    ApiUsage.completion_tokens == gateway_row.completion_tokens,
                    ApiUsage.meta_data["import_source"].astext == OTLP_SOURCE,
                    ApiUsage.meta_data["aggregate"].astext.is_(None),
                )
                .limit(20)
            ).scalars()
        )
        person_email = meta.get("gateway_subject_email")
        if not meta.get("gateway_subject_id") and gateway_row.user_id is not None:
            owner = db.get(User, gateway_row.user_id)
            person_email = owner.email if owner is not None else None
        named = bool(meta.get("gateway_subject_id"))
        matches = []
        for row in candidates:
            row_meta = row.meta_data if isinstance(row.meta_data, dict) else {}
            identity = (row_meta.get("telemetry") or {}).get("identity") or {}
            row_email = identity.get("user.email")
            if row_email:
                if (
                    not person_email
                    or str(row_email).lower() != str(person_email).lower()
                ):
                    continue
            elif named:
                continue
            if not _same_model(gateway_row.model_alias, row.model_alias):
                continue
            matches.append(row)
        if not matches:
            return []
        closest = min(
            matches, key=lambda row: abs((row.timestamp - when).total_seconds())
        )
        return [closest]

    def identity_has_gateway_rows(
        self,
        db: Session,
        *,
        account_id: Any,
        email: Optional[str],
        external_subject: Optional[str],
        hour_start: datetime,
    ) -> bool:
        """Whether the person has gateway rows in that UTC hour.

        Metrics carry no request id and, behind the apps gateway, gateway
        rows carry no client session id, so a session cannot be matched.
        When the same person already has gateway rows in the hour, metric
        aggregates would most likely count the same requests again, so
        they are not created (an undercount is preferred to a double count).
        """
        identity = self._identity_clause(account_id, email, external_subject)
        if identity is None:
            return False
        return bool(
            db.execute(
                select(
                    exists().where(
                        ApiUsage.account_id == account_id,
                        ApiUsage.action_type == GATEWAY_ACTION,
                        ApiUsage.timestamp >= hour_start,
                        ApiUsage.timestamp < hour_start + timedelta(hours=1),
                        identity,
                    )
                )
            ).scalar()
        )

    # --- Sessions -----------------------------------------------------------

    @staticmethod
    def find_runtime_session(
        db: Session, *, account_id: Any, session_id: str
    ) -> Optional[RuntimeSession]:
        """Return the gateway session a client ``session.id`` belongs to.

        Gateway requests key a Claude Code session as
        ``<api key id>:<x-claude-code-session-id>`` (#1429), or the bare id
        for sources without a principal prefix. Session ids are UUIDs, so a
        suffix match after ``:`` cannot collide with another session.
        """
        return db.execute(
            select(RuntimeSession)
            .where(
                RuntimeSession.account_id == account_id,
                or_(
                    RuntimeSession.session_source_id == session_id,
                    RuntimeSession.session_source_id.like(
                        f"%:{_escape_like(session_id)}", escape="\\"
                    ),
                ),
            )
            .order_by(RuntimeSession.last_activity_at.desc().nullslast())
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def session_has_gateway_rows(
        db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> bool:
        """Return whether gateway traffic was recorded for a session."""
        return bool(
            db.execute(
                select(
                    exists().where(
                        ApiUsage.account_id == account_id,
                        ApiUsage.runtime_session_id == runtime_session_id,
                        ApiUsage.action_type == GATEWAY_ACTION,
                    )
                )
            ).scalar()
        )

    @staticmethod
    def get_imported_by_fingerprint(
        db: Session, *, account_id: Any, fingerprint: str
    ) -> Optional[ApiUsage]:
        """Return the imported row with ``import_fingerprint`` (indexed)."""
        return db.execute(
            select(ApiUsage).where(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == IMPORTED_ACTION,
                ApiUsage.meta_data["import_fingerprint"].astext == fingerprint,
            )
        ).scalar_one_or_none()

    @staticmethod
    def delete_session_aggregates(
        db: Session, *, account_id: Any, session_id: str
    ) -> int:
        """Delete metric aggregates of a session once its log events arrive.

        Per-request log rows are the finer record, so aggregates built from
        the same session's metrics would count the spend twice.
        """
        rows: Iterable[ApiUsage] = db.execute(
            select(ApiUsage).where(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == IMPORTED_ACTION,
                ApiUsage.conversation_id == session_id,
                and_(
                    ApiUsage.meta_data["import_source"].astext == OTLP_SOURCE,
                    ApiUsage.meta_data["aggregate"].astext == "true",
                ),
            )
        ).scalars()
        count = 0
        for row in rows:
            db.delete(row)
            count += 1
        if count:
            db.flush()
        return count


_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _may_have_telemetry(meta: dict[str, Any]) -> bool:
    """Whether a gateway row can have an OTLP counterpart (a Claude client)."""
    return bool(
        meta.get("client_request_id")
        or meta.get("client") in CLAUDE_CLIENTS
        or meta.get("gateway_source") == APPS_GATEWAY_SOURCE
    )


def _same_model(left: Optional[str], right: Optional[str]) -> bool:
    """Same model, allowing only a date suffix (``x`` vs ``x-20250929``).

    Any other hyphenated extension is a different model (``claude-opus-4``
    vs ``claude-opus-4-1``, ``gpt-4o`` vs ``gpt-4o-mini``). Two different
    dates are different snapshots.
    """
    if not left or not right:
        return False
    left, right = left.lower(), right.lower()
    if left == right:
        return True
    left_base, right_base = _DATE_SUFFIX.sub("", left), _DATE_SUFFIX.sub("", right)
    if left_base != right_base:
        return False
    # Equal bases: same model when at most one side carries a date.
    return left == left_base or right == right_base


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
