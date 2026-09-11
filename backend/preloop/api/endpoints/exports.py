"""DORA agent-slice exports: the ICT asset register and incident candidates.

Two GETs that hand an auditor a file. The building is in
:mod:`preloop.services.dora_exports`; this module is the HTTP edge: it parses
the query, gates on the same permission that guards the audit trail, sets the
digest headers, and records that the export happened.

Both endpoints are reads of an entire account's compliance record, so both are
gated on ``view_audit_logs`` and both write an audit row of their own. See the
service module for scope (the agent slice only) and for why the incident
export is called *candidates* (classification is the financial entity's).
"""

import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.services.dora_exports import (
    AUDIT_ACTION_ASSET_EXPORT,
    AUDIT_ACTION_INCIDENT_EXPORT,
    DoraExport,
    DoraExportError,
    audit_dora_export,
    build_asset_register,
    build_incident_candidates,
    served_body,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exports", tags=["DORA Exports"])

#: Same permission as the audit trail. These exports are a bulk read of the
#: same records with a compliance-shaped projection, so anyone who may read
#: the trail may take the file, and nobody else.
EXPORT_PERMISSION = "view_audit_logs"

#: Default window for incident candidates when the caller gives neither
#: bound: the last 30 days, which is a plausible review period and small
#: enough that an unparameterised click cannot pull years of rows.
DEFAULT_PERIOD_DAYS = 30

#: How far a single incident export may reach. Wider than any quarterly
#: review; past this, ask for the period you actually want.
MAX_PERIOD_DAYS = 400

try:  # pragma: no cover - depends on the proprietary plugin being installed
    from preloop.plugins.proprietary.rbac.permissions import require_permission
except ImportError:  # pragma: no cover - OSS build
    from preloop.utils.permissions import require_permission


def _parse_bound(value: Optional[str], name: str) -> Optional[datetime]:
    """Accept ``YYYY-MM-DD`` or a full ISO 8601 instant, always as UTC.

    A date alone is the common case from a CLI or a form, and it means the
    start of that day in UTC. Naive instants are read as UTC too, because the
    columns being filtered are stored in UTC.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} must be YYYY-MM-DD or an ISO 8601 timestamp, got {value!r}"
                ),
            ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolve_period(
    from_: Optional[str], to: Optional[str], now: datetime
) -> tuple[datetime, datetime]:
    """Turn the two query params into a half-open [start, end) window."""
    start = _parse_bound(from_, "from")
    end = _parse_bound(to, "to")
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=DEFAULT_PERIOD_DAYS)
    if end <= start:
        raise HTTPException(status_code=400, detail="'to' must be after 'from'")
    if end - start > timedelta(days=MAX_PERIOD_DAYS):
        raise HTTPException(
            status_code=400,
            detail=(
                f"period is longer than {MAX_PERIOD_DAYS} days; request the "
                "review period you need, in parts if necessary"
            ),
        )
    return start, end


def _error_status(error: DoraExportError) -> int:
    """413 for an over-size export, 400 for anything the caller asked wrong."""
    return 413 if error.code == "export_too_large" else 400


def _respond(export: DoraExport) -> Response:
    """Serve the export, with the manifest reachable in both formats.

    JSON carries the manifest in the body next to the rows it digests. CSV
    cannot, so the manifest rides in a header: the two digests unwrapped for
    a shell one-liner, and the whole document base64'd (canonical JSON, the
    bytes that were hashed) for a verifier that wants all of it.
    """
    manifest = export.manifest
    body = served_body(export)
    headers = {
        "Content-Disposition": f'attachment; filename="{export.filename}"',
        "X-Preloop-Export-Sha256": hashlib.sha256(body).hexdigest(),
        "X-Preloop-Members-Digest": str(manifest.get("members_digest") or ""),
        "X-Preloop-Export-Rows": str(len(export.rows)),
        "X-Preloop-Export-Edition": str(
            (manifest.get("edition") or {}).get("edition") or "unknown"
        ),
    }
    if export.export_format == "csv":
        headers["X-Preloop-Export-Manifest"] = base64.b64encode(
            canonical_manifest_json(manifest)
        ).decode("ascii")
    return Response(content=body, media_type=export.media_type, headers=headers)


@router.get("/asset-register")
@require_permission(EXPORT_PERMISSION)
def export_asset_register(
    format: str = Query("csv", description="csv or json"),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
    account: models.Account = Depends(get_account_for_user),
) -> Response:
    """The account's agent-slice ICT assets as one flat register.

    Feeds an Art. 8 asset inventory and the ICT-service lines of an Art. 28
    register of information. It is an input to those documents, not either of
    them: Preloop knows the agents, tools, MCP servers, models, providers and
    runner hosts, and nothing else in the estate.
    """
    try:
        export = build_asset_register(db, account=account, export_format=format)
    except DoraExportError as error:
        raise HTTPException(
            status_code=_error_status(error), detail=str(error)
        ) from error
    audit_dora_export(
        db,
        account_id=account.id,
        user_id=current_user.id,
        export=export,
        action=AUDIT_ACTION_ASSET_EXPORT,
    )
    return _respond(export)


@router.get("/incident-candidates")
@require_permission(EXPORT_PERMISSION)
def export_incident_candidates(
    from_: Optional[str] = Query(
        None, alias="from", description="Start of the period, inclusive"
    ),
    to: Optional[str] = Query(None, description="End of the period, exclusive"),
    format: str = Query("csv", description="csv or json"),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
    account: models.Account = Depends(get_account_for_user),
) -> Response:
    """Everything in the period that might need classifying under Art. 17.

    Candidates, not incidents. Preloop reports what its own records show
    (failed executions, kill-switch activations, policy denies where a
    deployment persists them, budget denials, gateway upstream failures) with
    timestamps, correlation ids and the affected agent. Whether any of it is
    an ICT-related incident, and whether that incident is major, is the
    financial entity's determination under Art. 17 to 19.
    """
    start, end = _resolve_period(from_, to, datetime.now(UTC))
    try:
        export = build_incident_candidates(
            db, account=account, start=start, end=end, export_format=format
        )
    except DoraExportError as error:
        raise HTTPException(
            status_code=_error_status(error), detail=str(error)
        ) from error
    audit_dora_export(
        db,
        account_id=account.id,
        user_id=current_user.id,
        export=export,
        action=AUDIT_ACTION_INCIDENT_EXPORT,
    )
    return _respond(export)
