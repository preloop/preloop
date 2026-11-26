"""Account and device scoped terminal metadata reconciliation."""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from preloop.models import models


def _instant(value: str) -> datetime:
    """Parse a normalized report timestamp as an aware instant."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def reconcile(
    db: Session,
    *,
    runner: models.FlowRunner,
    report: dict[str, Any],
    cached: bool = False,
    affected: set[str] | None = None,
) -> dict[str, Any]:
    """Join exact native ids only through an enrolled device/principal link.

    Unknown device provenance stays unmanaged. Labels and hostname are never
    identity evidence. An ambiguous native id is also left unmanaged.
    """
    if affected is None:
        affected = set()
    fingerprint = runner.workstation_fingerprint
    agents = []
    seen: set[str] = set()
    observed = _instant(report["observed_at"])
    identities = Counter(
        (pane["harness_kind"], pane["harness_session_id"]) for pane in report["agents"]
    )
    ambiguous_panes = {
        pane["pane_id"]
        for pane in report["agents"]
        if identities[(pane["harness_kind"], pane["harness_session_id"])] > 1
    }
    for pane in report["agents"]:
        if cached and pane.get("managed"):
            agents.append(pane)
            continue
        pane = {**pane, "managed": False, "runtime_session_id": None}
        kind = {"claude_code": "claude_code", "codex_cli": "codex"}.get(
            pane["harness_kind"]
        )
        native = pane["harness_session_id"]
        rows = []
        if (
            fingerprint
            and kind
            and native
            and identities[(pane["harness_kind"], native)] == 1
        ):
            session, agent, candidate = (
                models.RuntimeSession,
                models.ManagedAgent,
                models.DiscoveredAgentCandidate,
            )
            rows = (
                db.query(session)
                .join(
                    agent,
                    and_(
                        agent.account_id == session.account_id,
                        agent.session_source_type == session.runtime_principal_type,
                        or_(
                            agent.session_source_id == session.runtime_principal_id,
                            agent.session_source_id + ":" + native
                            == session.runtime_principal_id,
                        ),
                    ),
                )
                .join(
                    candidate,
                    and_(
                        candidate.managed_agent_id == agent.id,
                        candidate.account_id == agent.account_id,
                    ),
                )
                .filter(
                    session.account_id == runner.account_id,
                    candidate.workstation_fingerprint == fingerprint,
                    session.runtime_principal_type == kind,
                    or_(
                        session.session_reference == native,
                        session.session_source_id
                        == agent.session_source_id + ":" + native,
                    ),
                )
                .distinct()
                .all()
            )
        if len(rows) == 1:
            row = rows[0]
            seen.add(str(row.id))
            previous = row.terminal_location or {}
            if not previous or _instant(previous["observed_at"]) <= observed:
                location = {
                    key: pane[key]
                    for key in (
                        "multiplexer",
                        "workspace_id",
                        "workspace_label",
                        "tab_id",
                        "tab_label",
                        "pane_id",
                        "pane_label",
                        "terminal_title",
                        "state",
                    )
                } | {
                    "device_id": fingerprint,
                    "observed_at": report["observed_at"],
                    "pane_closed_at": report["observed_at"]
                    if pane["state"] == "closed"
                    else None,
                }
                if location != previous:
                    row.terminal_location = location
                    db.add(row)
                    affected.add(str(row.id))
            pane.update(managed=True, runtime_session_id=str(row.id))
        agents.append(pane)
    if fingerprint and not cached:
        for row in (
            db.query(models.RuntimeSession)
            .filter(
                models.RuntimeSession.account_id == runner.account_id,
                models.RuntimeSession.terminal_location["device_id"].astext
                == fingerprint,
                models.RuntimeSession.terminal_location["multiplexer"].astext
                == "herdr",
            )
            .all()
        ):
            previous = row.terminal_location
            if previous["pane_id"] in ambiguous_panes:
                continue
            if (
                str(row.id) not in seen
                and _instant(previous["observed_at"]) <= observed
            ):
                location = previous | {
                    "state": "closed",
                    "pane_closed_at": previous.get("pane_closed_at")
                    or report["observed_at"],
                    "observed_at": report["observed_at"],
                }
                if location != previous:
                    row.terminal_location = location
                    db.add(row)
                    affected.add(str(row.id))
    db.flush()
    return report | {"agents": agents}


def clear_device_locations(db: Session, *, runner: models.FlowRunner) -> set[str]:
    """Observation disabled: forget locations without claiming panes closed."""
    affected: set[str] = set()
    if not runner.workstation_fingerprint:
        return affected
    for row in (
        db.query(models.RuntimeSession)
        .filter(
            models.RuntimeSession.account_id == runner.account_id,
            models.RuntimeSession.terminal_location["device_id"].astext
            == runner.workstation_fingerprint,
            models.RuntimeSession.terminal_location["multiplexer"].astext == "herdr",
        )
        .all()
    ):
        row.terminal_location = None
        db.add(row)
        affected.add(str(row.id))
    db.flush()
    return affected
