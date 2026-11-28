"""Bounded, read-only execution attribution and destination evidence."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from preloop.models import models

DEFINITION_PAGE_SIZE = 16384
SELECTION_PAGE_SIZE = 16


def _project() -> Any:
    """Prefer the controller-bound project for CI and tracker review executions."""
    run = models.FlowExecution
    return func.coalesce(
        run.ci_review_binding["project_id"].astext,
        run.trigger_event_details["project_id"].astext,
    )


def _owned(
    statement: Select, execution_id: UUID, account_id: UUID, project_id: UUID | None
) -> Select:
    """Apply execution ownership and the requested project scope to a projection."""
    run = models.FlowExecution
    statement = (
        statement.select_from(run)
        .join(models.Flow, models.Flow.id == run.flow_id)
        .where(run.id == execution_id, models.Flow.account_id == account_id)
    )
    if project_id is not None:
        statement = statement.where(_project() == str(project_id))
    return statement


def _selections(
    statement: Select, execution_id: UUID, account_id: UUID, project_id: UUID | None
) -> Select:
    """Read only revisions actually selected for this owned execution."""
    selection = models.ExecutionConfigurationSelection
    snap = models.FlowDefinitionSnapshot
    return (
        _owned(statement, execution_id, account_id, project_id)
        .join(
            selection,
            selection.execution_id == models.FlowExecution.id,
        )
        .join(
            snap,
            and_(
                snap.id == selection.snapshot_id,
                snap.flow_id == models.FlowExecution.flow_id,
                snap.account_id == account_id,
            ),
        )
    )


def definition_page(
    db: Session,
    *,
    execution_id: UUID,
    account_id: UUID,
    project_id: UUID | None,
    revision: int,
    offset: int,
) -> dict[str, Any] | None:
    """Return a window of the stored ASCII JSON, including its continuation offset."""
    snap = models.FlowDefinitionSnapshot
    row = db.execute(
        _selections(
            select(
                models.FlowExecution.flow_id,
                func.left(_project(), 128),
                func.substr(snap.definition, offset + 1, DEFINITION_PAGE_SIZE + 1),
            ),
            execution_id,
            account_id,
            project_id,
        )
        .where(snap.revision == revision)
        .limit(1)
    ).first()
    if row is None:
        return None
    flow_id, project, page = row
    return {
        "flow_id": flow_id,
        "project_id": project,
        "revision": revision,
        "definition_json": page[:DEFINITION_PAGE_SIZE],
        "next_offset": offset + DEFINITION_PAGE_SIZE
        if len(page) > DEFINITION_PAGE_SIZE
        else None,
    }


def read(
    db: Session,
    *,
    execution_id: UUID,
    account_id: UUID,
    project_id: UUID | None,
    offset: int,
) -> dict[str, Any] | None:
    """Read scalar work and capture facts, recorded selections and provider proof."""
    from preloop.services.execution_definition import public_text
    from preloop.services.review_publication import parse_pull_request_url

    run = models.FlowExecution
    binding = run.ci_review_binding
    details = run.trigger_event_details
    subject = details["_subject"]
    payload = details["payload"]
    finished = run.startup_receipt["_completion"]
    repo = func.coalesce(
        binding["repository_slug"].astext,
        payload["repository"]["full_name"].astext,
        payload["project"]["path_with_namespace"].astext,
        subject["repo"].astext,
    )
    number = func.coalesce(
        binding["pr_number"].astext,
        payload["pull_request"]["number"].astext,
        payload["pullrequest"]["id"].astext,
        case(
            (
                payload["object_kind"].astext == "merge_request",
                payload["object_attributes"]["iid"].astext,
            )
        ),
    )
    head = func.coalesce(
        binding["head_sha"].astext,
        payload["pull_request"]["head"]["sha"].astext,
        payload["object_attributes"]["last_commit"]["id"].astext,
        payload["pullrequest"]["source"]["commit"]["hash"].astext,
    )
    provider = func.coalesce(binding["tracker_type"].astext, details["source"].astext)
    row = db.execute(
        _owned(
            select(
                run.flow_id,
                run.flow_revision,
                run.retry_of_execution_id,
                func.left(run.status, 32).label("status"),
                func.left(_project(), 128).label("project_id"),
                func.left(run.trigger_event_id, 256).label("event_id"),
                func.left(subject["reference"].astext, 128).label("reference"),
                func.left(
                    func.coalesce(
                        payload["object_attributes"]["url"].astext,
                        payload["pull_request"]["html_url"].astext,
                        payload["pullrequest"]["links"]["html"]["href"].astext,
                    ),
                    2049,
                ).label("pr_url"),
                func.left(repo, 513).label("repo"),
                func.left(number, 65).label("number"),
                func.left(head, 65).label("head"),
                func.left(provider, 64).label("provider"),
                func.left(finished["capture_state"].astext, 16).label("capture"),
                case(
                    (
                        func.jsonb_typeof(run.result_schema_validation["valid"])
                        == "boolean",
                        run.result_schema_validation["valid"],
                    )
                ).label("valid"),
                func.left(finished["agent_finished"]["status"].astext, 32).label(
                    "agent_status"
                ),
                func.left(
                    details["_model_routing"]["executed"]["agent_type"].astext, 64
                ).label("agent_type"),
                func.left(
                    details["_model_routing"]["executed"]["ai_model_id"].astext, 64
                ).label("model"),
            ),
            execution_id,
            account_id,
            project_id,
        )
    ).one_or_none()
    if row is None:
        return None

    selection = models.ExecutionConfigurationSelection
    snap = models.FlowDefinitionSnapshot
    rows = db.execute(
        _selections(
            select(selection.stage, snap.revision), execution_id, account_id, project_id
        )
        .order_by(selection.created_at, selection.stage, snap.revision)
        .offset(offset)
        .limit(SELECTION_PAGE_SIZE + 1)
    ).all()
    artifact = models.FlowArtifact
    artifact_id = db.scalar(
        select(artifact.id)
        .where(
            artifact.account_id == account_id,
            artifact.flow_id == row.flow_id,
            artifact.execution_id == execution_id,
            artifact.kind == "evidence",
        )
        .order_by(artifact.created_at.desc(), artifact.id.desc())
        .limit(1)
    )
    repository, pull_request = row.repo, row.number
    if not (repository and pull_request) and row.pr_url and len(row.pr_url) <= 2048:
        try:
            target, number_from_url = parse_pull_request_url(row.pr_url)
        except ValueError:
            target, number_from_url = None, None
        if target and (not repository or repository.lower() == target):
            repository, pull_request = target, number_from_url
    destination = _review_destination(
        db, account_id, execution_id, repository, pull_request, row.head, row.provider
    ) or _published_pull_request(db, execution_id, account_id, project_id)
    return {
        "execution_id": execution_id,
        "flow_id": row.flow_id,
        "creation_revision": row.flow_revision,
        "retry_of_execution_id": row.retry_of_execution_id,
        "configurations": [dict(r._mapping) for r in rows[:SELECTION_PAGE_SIZE]],
        "next_offset": offset + SELECTION_PAGE_SIZE
        if len(rows) > SELECTION_PAGE_SIZE
        else None,
        "work": {
            key: public_text(val) if val else None
            for key, val in (
                ("project_id", row.project_id),
                ("event_id", row.event_id),
                ("repository", repository),
                ("reference", row.reference),
                ("pull_request", pull_request),
                ("head_sha", row.head),
            )
        },
        "runtime": {
            "execution_status": row.status,
            "agent_status": row.agent_status
            if row.agent_status
            in {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "CANCELLED"}
            else None,
            "agent_type": public_text(row.agent_type) if row.agent_type else None,
            "ai_model_id": public_text(row.model) if row.model else None,
        },
        "output": {
            "capture": row.capture
            if row.capture in {"absent", "pending", "captured"}
            else "unknown",
            "validation": "valid"
            if row.valid is True
            else "failed"
            if row.valid is False
            else "unknown",
            "evidence_artifact_id": artifact_id,
        },
        "delivery": {
            "state": "delivered" if destination else "unconfirmed",
            "destination": destination,
        },
    }


def _review_destination(
    db: Session,
    account_id: UUID,
    execution_id: UUID,
    repository: str | None,
    number: str | None,
    head: str | None,
    provider: str | None,
) -> dict[str, Any] | None:
    """Find provider evidence for this run's own review target."""
    if (
        not repository
        or not number
        or provider not in {"github", "gitlab", "bitbucket"}
    ):
        return None
    from preloop.services.execution_definition import public_text

    receipt = models.ReviewPublicationReceipt
    platform = models.PlatformReviewPublication
    for model in (platform, receipt):
        query = select(
            model.id,
            model.head_sha,
            model.review_id if model is platform else model.provider_review_id,
        ).where(
            model.account_id == account_id,
            model.execution_id == execution_id,
            model.repository == repository.lower(),
            model.pull_request == number,
        )
        if head:
            query = query.where(model.head_sha == head.lower())
        if model is platform:
            query = query.where(model.state == "posted", model.review_state == "posted")
        else:
            query = query.where(model.provider == provider)
        row = db.execute(
            query.order_by(model.created_at.desc(), model.id.desc()).limit(1)
        ).first()
        if row:
            return {
                "kind": "pull_request_review",
                "provider": provider,
                "repository": public_text(repository),
                "pull_request": public_text(number),
                "head_sha": public_text(row[1]) if row[1] else None,
                "provider_review_id": public_text(row[2]) if row[2] else None,
                "record_id": row[0],
                "source": model.__tablename__,
            }
    return None


def _published_pull_request(
    db: Session, execution_id: UUID, account_id: UUID, project_id: UUID | None
) -> dict[str, Any] | None:
    """Project one destination from a fully acknowledged isolated publication."""
    from preloop.services.execution_definition import public_text

    confirmed = models.FlowExecution.startup_receipt["_completion"][
        "published_pull_request"
    ]
    saved = db.execute(
        _owned(
            select(
                func.left(confirmed["provider"].astext, 32),
                func.left(confirmed["repository"].astext, 512),
                func.left(confirmed["pull_request"].astext, 64),
                func.left(confirmed["destination_count"].astext, 9),
            ),
            execution_id,
            account_id,
            project_id,
        ).where(confirmed["repository"].astext.isnot(None))
    ).first()
    if (
        saved
        and saved[0] in {"github", "gitlab", "bitbucket"}
        and saved[1]
        and saved[2]
        and saved[3]
        and saved[3].isascii()
        and saved[3].isdigit()
        and int(saved[3]) > 0
    ):
        return {
            "kind": "pull_request",
            "source": "controller_publication",
            "provider": saved[0],
            "repository": public_text(saved[1]),
            "pull_request": public_text(saved[2]),
            "destination_count": int(saved[3]),
        }
    # Older private publishers already persisted an authenticated completion.
    result = models.FlowExecution.result
    state = result["_private_publication"]
    receipt = state["receipt"]
    repositories = receipt["repositories"]
    first = repositories[0]
    query = _owned(
        select(
            func.left(first["repository_url"].astext, 1024),
            func.left(first["number"].astext, 64),
            func.jsonb_array_length(repositories),
        ),
        execution_id,
        account_id,
        project_id,
    ).where(
        func.jsonb_typeof(repositories) == "array",
        state["phase"].astext == "complete",
        receipt == result["trusted_publication"],
        receipt["status"].astext == "complete",
        first["status"].astext == "published",
        first["provider"].astext == "github",
    )
    row = db.execute(query).first()
    if row is None or not row[0] or not row[1]:
        return None
    return {
        "kind": "pull_request",
        "source": "private_publication_receipt",
        "provider": "github",
        "repository": public_text(
            row[0].removeprefix("https://github.com/").removesuffix(".git")
        ),
        "pull_request": row[1],
        "destination_count": row[2],
    }
