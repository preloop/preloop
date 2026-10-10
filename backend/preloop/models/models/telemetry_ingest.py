"""State for OTLP telemetry ingest (issue #1412).

Two small tables back the OTLP/HTTP receiver that turns Claude client
telemetry into usage rows:

* :class:`TelemetryIngestDedup` holds one sha256 key per stored record, so a
  batch an exporter re-sends after a timeout is a no-op. The unique index on
  ``(account_id, dedup_key)`` is the guard, not an application check.
* :class:`TelemetryMetricSeries` keeps the last cumulative value per metric
  series, so cumulative exports can be turned into deltas. Rows older than
  24 hours are treated as absent and pruned.

Neither table stores attribute values beyond the allowlisted series key
digest. No prompt, command or file path ever reaches them.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TelemetryIngestDedup(Base):
    """One claimed dedup key per stored OTLP record.

    Attributes:
        account_id: Account of the ingest key.
        dedup_key: sha256 hex over the record identity (see
            ``preloop.services.otlp_telemetry.record_dedup_key``).
        kind: What the key stands for: ``log``, ``metric`` or ``session_logs``
            (a marker that a session has exported log events).
        seen_at: When the key was claimed (UTC).
    """

    __tablename__ = "telemetry_ingest_dedup"
    __table_args__ = (
        Index(
            "uq_telemetry_ingest_dedup_account_key",
            "account_id",
            "dedup_key",
            unique=True,
        ),
        Index("ix_telemetry_ingest_dedup_seen_at", "seen_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TelemetryMetricSeries(Base):
    """Last cumulative value of one metric series, for delta conversion.

    Attributes:
        account_id: Account of the ingest key.
        series_key: sha256 hex over metric name, resource and series
            attributes and the series start time.
        last_value: Cumulative value of the most recent data point.
        last_time: Time of the most recent data point (UTC).
        start_time: Series start time reported by the exporter, if any.
    """

    __tablename__ = "telemetry_metric_series"
    __table_args__ = (
        Index(
            "uq_telemetry_metric_series_account_key",
            "account_id",
            "series_key",
            unique=True,
        ),
        Index("ix_telemetry_metric_series_updated_at", "updated_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    series_key: Mapped[str] = mapped_column(String(64), nullable=False)
    last_value: Mapped[float] = mapped_column(Float, nullable=False)
    last_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
