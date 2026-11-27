"""Repository observations from this agent's native hook history."""

from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models


def observed_checkouts(
    db: Session, *, account_id: Any, agent: Any
) -> list[dict[str, str]]:
    """Read bounded checkout paths already recorded by hooks on this host."""
    session = models.RuntimeSession
    approval = models.ApprovalRequest
    rows = (
        db.query(approval.tool_args["_preloop_repository"])
        .join(session, approval.runtime_session_id == session.id)
        .filter(
            approval.account_id == account_id,
            session.account_id == account_id,
            session.runtime_principal_type == agent.session_source_type,
            (session.runtime_principal_id == agent.session_source_id)
            | session.runtime_principal_id.startswith(
                agent.session_source_id + ":", autoescape=True
            ),
            approval.tool_args["_preloop_repository"].isnot(None),
        )
        .order_by(approval.created_at.desc())
        .limit(100)
        .all()
    )
    results = []
    for (repository,) in rows:
        if not isinstance(repository, dict) or repository.get("source") != "hook_cwd":
            continue
        directory, remote = repository.get("toplevel"), repository.get("remote")
        if isinstance(directory, str) and isinstance(remote, str):
            results.append({"path": directory, "remote": remote})
    return results
