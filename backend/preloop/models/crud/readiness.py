"""Transactional policy checks and account-scoped sampled evidence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.readiness import (
    ReadinessObservation,
    ReadinessPolicy,
    TicketCreationEvidence,
)


class PolicyChangedError(ValueError):
    """A captured policy ceased to be active before the write transaction."""


def _project(
    db: Session, account_id: UUID, project_id: UUID, *, lock: bool = True
) -> models.Project | None:
    query = (
        select(models.Project)
        .join(models.Organization)
        .join(models.Tracker)
        .where(models.Project.id == project_id, models.Tracker.account_id == account_id)
    )
    return db.scalar(query.with_for_update(of=models.Project) if lock else query)


def active_policy(
    db: Session, *, account_id: UUID, project_id: UUID, lock: bool = False
) -> ReadinessPolicy | None:
    """Read the immutable revision named by this account's project."""
    project = _project(db, account_id, project_id, lock=lock)
    if project is None:
        return None
    row = db.scalar(
        select(models.ReadinessPolicyRecord).where(
            models.ReadinessPolicyRecord.account_id == account_id,
            models.ReadinessPolicyRecord.project_id == project_id,
            models.ReadinessPolicyRecord.is_active.is_(True),
        )
    )
    return ReadinessPolicy.model_validate(row.configuration) if row else None


def activate_policy(
    db: Session, *, account_id: UUID, project_id: UUID, policy: ReadinessPolicy | None
) -> None:
    """Activate a new version under the same lock used by observation writes."""
    project = _project(db, account_id, project_id)
    if project is None:
        raise LookupError("Project not found")
    from sqlalchemy import update

    db.execute(
        update(models.ReadinessPolicyRecord)
        .where(
            models.ReadinessPolicyRecord.account_id == account_id,
            models.ReadinessPolicyRecord.project_id == project_id,
            models.ReadinessPolicyRecord.is_active.is_(True),
        )
        .values(is_active=False)
    )
    if policy is not None:
        db.add(
            models.ReadinessPolicyRecord(
                id=policy.version,
                account_id=account_id,
                project_id=project_id,
                is_active=True,
                configuration=policy.model_dump(mode="json"),
            )
        )
    # Revisions/opt-in wake existing jobs without carrying an old series.
    if policy is not None:
        from datetime import UTC

        ids = (
            select(models.IssueCostPullRequest.id)
            .join(
                models.IssueCostRollup,
                models.IssueCostPullRequest.rollup_id == models.IssueCostRollup.id,
            )
            .where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.merged_at.is_(None),
                models.IssueCostRollup.project_id == project_id,
                models.IssueCostRollup.account_id == account_id,
            )
        )
        db.execute(
            update(models.ReadinessJob)
            .where(
                models.ReadinessJob.account_id == account_id,
                models.ReadinessJob.pr_id.in_(ids),
            )
            .values(closed=False, due_at=datetime.now(UTC))
        )
    db.flush()


def persist_observation(
    db: Session,
    *,
    account_id: UUID,
    pr_record_id: UUID,
    observation: ReadinessObservation,
) -> models.ReadinessObservationRecord:
    """CAS against captured active policy; replay cannot rewrite the first fact."""
    if observation.account_id != account_id:
        raise ValueError("cross_account")
    pr = db.scalar(
        select(models.IssueCostPullRequest)
        .where(
            models.IssueCostPullRequest.id == pr_record_id,
            models.IssueCostPullRequest.account_id == account_id,
        )
        .with_for_update()
    )
    if pr is None or pr.ambiguous or pr.rollup_id is None:
        raise ValueError("missing_binding")
    rollup = db.scalar(
        select(models.IssueCostRollup).where(
            models.IssueCostRollup.id == pr.rollup_id,
            models.IssueCostRollup.account_id == account_id,
        )
    )
    if rollup is None or rollup.project_id is None:
        raise ValueError("missing_binding")
    policy = active_policy(
        db, account_id=account_id, project_id=rollup.project_id, lock=True
    )
    if (policy.version if policy else None) != observation.policy_version:
        raise PolicyChangedError("policy_changed")
    if policy is not None and observation.coverage == "complete":
        required = {"open", "non_draft", "approvals", "conflict"}
        required.update(f"build:{key}" for key in policy.required_build_keys)
        if policy.changes_requests_block:
            required.add("changes_requests")
        if policy.unresolved_tasks_block:
            required.add("tasks")
        gates = {gate.name: gate for gate in observation.gates}
        if (
            set(gates) != required
            or len(gates) != len(observation.gates)
            or any(gate.state == "unknown" for gate in gates.values())
        ):
            raise ValueError("incomplete_gate_evidence")
        expected_state = (
            "not_ready"
            if any(gate.state == "fail" for gate in gates.values())
            else "ready"
        )
        if observation.state != expected_state:
            raise ValueError("inconsistent_gate_evidence")
    context = observation_context(db, account_id=account_id, pr_record_id=pr_record_id)
    if (
        context is None
        or observation.tracker_id != context[3].id
        or observation.repository != context[4]
        or observation.pr_id != context[5]
    ):
        raise ValueError("cross_account_or_repository")
    existing = db.get(models.ReadinessObservationRecord, observation.observation_id)
    if existing:
        if (
            existing.account_id != account_id
            or existing.pr_id != pr_record_id
            or existing.evidence != observation.model_dump(mode="json")
        ):
            raise ValueError("observation_identity_conflict")
        return existing
    row = models.ReadinessObservationRecord(
        id=observation.observation_id,
        account_id=account_id,
        pr_id=pr_record_id,
        policy_id=observation.policy_version,
        completed_at=observation.completed_at,
        state=observation.state,
        evidence=observation.model_dump(mode="json"),
    )
    db.add(row)
    db.flush()
    if policy:
        series = db.scalar(
            select(models.ReadinessSeries).where(
                models.ReadinessSeries.account_id == account_id,
                models.ReadinessSeries.pr_id == pr_record_id,
                models.ReadinessSeries.policy_id == policy.version,
            )
        )
        if series is None:
            series = models.ReadinessSeries(
                account_id=account_id,
                pr_id=pr_record_id,
                policy_id=policy.version,
                latest_id=row.id,
            )
            db.add(series)
        else:
            latest = db.get(models.ReadinessObservationRecord, series.latest_id)
            if latest and latest.completed_at > row.completed_at:
                # An older completion may be audited but cannot overwrite the
                # latest sampled state or create a historical backfill fact.
                db.flush()
                return row
            series.latest_id = row.id
        if (
            series.first_ready_id is None
            and observation.state == "ready"
            and observation.coverage == "complete"
        ):
            series.first_ready_id = row.id
        db.flush()
    return row


def persist_ticket_creation(
    db: Session, *, account_id: UUID, rollup_id: UUID, evidence: TicketCreationEvidence
) -> None:
    """Store only tracker-matched authoritative creation for this account."""
    rollup = db.scalar(
        select(models.IssueCostRollup)
        .where(
            models.IssueCostRollup.id == rollup_id,
            models.IssueCostRollup.account_id == account_id,
        )
        .with_for_update()
    )
    if (
        rollup is None
        or evidence.tracker_id != rollup.tracker_id
        or evidence.issue_key != rollup.issue_key
    ):
        raise ValueError("missing_binding")
    tracker = db.scalar(
        select(models.Tracker).where(
            models.Tracker.id == rollup.tracker_id,
            models.Tracker.account_id == account_id,
        )
    )
    if tracker is None or tracker.tracker_type != "jira":
        raise ValueError("unsupported_adapter")
    row = db.scalar(
        select(models.TicketCreationRecord).where(
            models.TicketCreationRecord.rollup_id == rollup_id,
            models.TicketCreationRecord.account_id == account_id,
        )
    )
    if row is None:
        db.add(
            models.TicketCreationRecord(
                account_id=account_id,
                rollup_id=rollup_id,
                evidence=evidence.model_dump(mode="json"),
            )
        )
    else:
        old = TicketCreationEvidence.model_validate(row.evidence)
        if old.retrieved_at <= evidence.retrieved_at:
            row.evidence = evidence.model_dump(mode="json")
    db.flush()


def report_evidence(
    db: Session, *, account_id: UUID, rollup_id: UUID
) -> tuple[
    TicketCreationEvidence | None,
    list[tuple[ReadinessObservation | None, ReadinessObservation | None]],
    str | None,
]:
    """Exactly one bound PR qualifies; multi-PR evidence stays per PR."""
    rollup = db.scalar(
        select(models.IssueCostRollup).where(
            models.IssueCostRollup.id == rollup_id,
            models.IssueCostRollup.account_id == account_id,
        )
    )
    if rollup is None:
        return None, [], "missing_binding"
    ticket = db.scalar(
        select(models.TicketCreationRecord).where(
            models.TicketCreationRecord.rollup_id == rollup_id,
            models.TicketCreationRecord.account_id == account_id,
        )
    )
    creation = (
        TicketCreationEvidence.model_validate(ticket.evidence) if ticket else None
    )
    policy = (
        active_policy(db, account_id=account_id, project_id=rollup.project_id)
        if rollup.project_id
        else None
    )
    if policy is None:
        return creation, [], "policy_unconfigured"
    prs = list(
        db.scalars(
            select(models.IssueCostPullRequest).where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.rollup_id == rollup_id,
            )
        )
    )
    observations = []
    for pr in prs:
        series = db.scalar(
            select(models.ReadinessSeries).where(
                models.ReadinessSeries.account_id == account_id,
                models.ReadinessSeries.pr_id == pr.id,
                models.ReadinessSeries.policy_id == policy.version,
            )
        )
        first = (
            db.get(models.ReadinessObservationRecord, series.first_ready_id)
            if series and series.first_ready_id
            else None
        )
        latest = (
            db.get(models.ReadinessObservationRecord, series.latest_id)
            if series
            else None
        )
        observations.append(
            (
                ReadinessObservation.model_validate(first.evidence) if first else None,
                ReadinessObservation.model_validate(latest.evidence)
                if latest
                else None,
            )
        )
    reason = (
        "ambiguous_pr"
        if len(prs) > 1 or any(p.ambiguous for p in prs)
        else "missing_binding"
        if not prs
        else None
    )
    return creation, observations, reason


def schedule(db: Session, *, account_id: UUID, pr_id: UUID, now: datetime) -> None:
    """Deduplicate events without disturbing an in-flight lease."""
    from sqlalchemy.dialects.postgresql import insert

    pr = db.scalar(
        select(models.IssueCostPullRequest).where(
            models.IssueCostPullRequest.id == pr_id,
            models.IssueCostPullRequest.account_id == account_id,
        )
    )
    if pr is None or pr.rollup_id is None or pr.ambiguous:
        return
    statement = insert(models.ReadinessJob).values(
        account_id=account_id, pr_id=pr_id, due_at=now, closed=pr.merged_at is not None
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["pr_id"],
            set_={"due_at": now, "closed": pr.merged_at is not None},
        )
    )


def reconcile(db: Session, *, now: datetime) -> int:
    """At most 100 open bound PRs per account per pass, with wraparound."""
    accounts = list(
        db.scalars(
            select(models.IssueCostPullRequest.account_id)
            .where(
                models.IssueCostPullRequest.rollup_id.is_not(None),
                models.IssueCostPullRequest.merged_at.is_(None),
                models.IssueCostPullRequest.ambiguous.is_(False),
            )
            .distinct()
        )
    )
    total = 0
    for account_id in accounts:
        cursor = db.scalar(
            select(models.ReadinessCursor)
            .where(models.ReadinessCursor.account_id == account_id)
            .with_for_update()
        )
        if cursor is None:
            cursor = models.ReadinessCursor(account_id=account_id)
            db.add(cursor)
            db.flush()
        query = (
            select(models.IssueCostPullRequest.id)
            .where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.rollup_id.is_not(None),
                models.IssueCostPullRequest.merged_at.is_(None),
                models.IssueCostPullRequest.ambiguous.is_(False),
            )
            .order_by(models.IssueCostPullRequest.id)
        )
        ids = (
            list(
                db.scalars(
                    query.where(models.IssueCostPullRequest.id > cursor.pr_id).limit(
                        100
                    )
                )
            )
            if cursor.pr_id
            else list(db.scalars(query.limit(100)))
        )
        if not ids:
            ids = list(db.scalars(query.limit(100)))
        for pr_id in ids:
            # Reconciliation does not postpone an earlier webhook/retry.
            existing = db.scalar(
                select(models.ReadinessJob).where(
                    models.ReadinessJob.account_id == account_id,
                    models.ReadinessJob.pr_id == pr_id,
                )
            )
            if existing is None:
                schedule(db, account_id=account_id, pr_id=pr_id, now=now)
        if ids:
            cursor.pr_id = ids[-1]
        total += len(ids)
    db.flush()
    return total


def claim(db: Session, *, now: datetime) -> models.ReadinessJob | None:
    """Claim one row atomically; crashed leases become eligible after three minutes."""
    from datetime import timedelta
    from uuid import uuid4
    from sqlalchemy import or_

    job = db.scalar(
        select(models.ReadinessJob)
        .where(
            models.ReadinessJob.closed.is_(False),
            models.ReadinessJob.due_at <= now,
            or_(
                models.ReadinessJob.lease_until.is_(None),
                models.ReadinessJob.lease_until < now,
            ),
        )
        .order_by(models.ReadinessJob.due_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job:
        job.lease_token = uuid4()
        job.lease_until = now + timedelta(minutes=3)
        # due_at is now the next normal pass; an event during this lease sets
        # it earlier and remains queued when finish releases the lease.
        job.due_at = now + timedelta(minutes=5)
        db.flush()
    return job


def finish(
    db: Session,
    *,
    job_id: UUID,
    token: UUID,
    closed: bool = False,
    retry: bool = False,
    now: datetime,
) -> bool:
    """An expired worker cannot release its replacement's observation lease."""
    from datetime import timedelta

    job = db.scalar(
        select(models.ReadinessJob)
        .where(
            models.ReadinessJob.id == job_id, models.ReadinessJob.lease_token == token
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        return False
    job.lease_token = None
    job.lease_until = None
    job.closed = closed
    if retry:
        job.due_at = now + timedelta(seconds=30)
    db.flush()
    return True


def observation_context(
    db: Session, *, account_id: UUID, pr_record_id: UUID
) -> (
    tuple[
        models.IssueCostPullRequest,
        models.IssueCostRollup,
        models.Tracker,
        models.Tracker,
        str,
        int,
        ReadinessPolicy | None,
    ]
    | None
):
    """Resolve authoritative Jira and one account-owned bound Bitbucket tracker."""
    from urllib.parse import urlsplit

    pr = db.scalar(
        select(models.IssueCostPullRequest).where(
            models.IssueCostPullRequest.id == pr_record_id,
            models.IssueCostPullRequest.account_id == account_id,
        )
    )
    if pr is None or pr.rollup_id is None or pr.ambiguous:
        return None
    rollup = db.scalar(
        select(models.IssueCostRollup).where(
            models.IssueCostRollup.id == pr.rollup_id,
            models.IssueCostRollup.account_id == account_id,
        )
    )
    if rollup is None or not rollup.project_id:
        return None
    project = db.scalar(
        select(models.Project).where(models.Project.id == rollup.project_id)
    )
    ticket_tracker = db.scalar(
        select(models.Tracker).where(
            models.Tracker.id == rollup.tracker_id,
            models.Tracker.account_id == account_id,
            models.Tracker.tracker_type == "jira",
        )
    )
    if project is None or ticket_tracker is None:
        return None
    url = urlsplit(pr.pr_key)
    parts = url.path.strip("/").split("/")
    if (
        url.scheme != "https"
        or url.netloc != "bitbucket.org"
        or len(parts) != 4
        or parts[2] != "pull-requests"
        or not parts[3].isdigit()
    ):
        return None
    repository = "/".join(parts[:2])
    # An execution's explicit flow bindings override the project's defaults.
    # Deduplicate equal bindings, and reject conflicting code-host identities.
    flow_configs = list(
        db.scalars(
            select(models.Flow.git_clone_config)
            .join(
                models.IssueCostExecution,
                models.IssueCostExecution.flow_id == models.Flow.id,
            )
            .where(
                models.Flow.account_id == account_id,
                models.IssueCostExecution.account_id == account_id,
                models.IssueCostExecution.rollup_id == rollup.id,
                models.IssueCostExecution.pr_key == pr.pr_key,
            )
        )
    )
    explicit = [
        binding
        for config in flow_configs
        if isinstance(config, dict)
        for binding in config.get("repository_bindings", [])
        if isinstance(binding, dict) and binding.get("repository") == repository
    ]
    candidates = explicit or [
        binding
        for binding in (project.settings or {}).get("repository_bindings", [])
        if isinstance(binding, dict) and binding.get("repository") == repository
    ]
    bindings = list(
        {str(binding.get("tracker_id")): binding for binding in candidates}.values()
    )
    if len(bindings) != 1:
        return None
    host_tracker = db.scalar(
        select(models.Tracker).where(
            models.Tracker.id == UUID(str(bindings[0]["tracker_id"])),
            models.Tracker.account_id == account_id,
            models.Tracker.tracker_type == "bitbucket",
        )
    )
    if host_tracker is None:
        return None
    policy = active_policy(db, account_id=account_id, project_id=project.id)
    return pr, rollup, ticket_tracker, host_tracker, repository, int(parts[3]), policy


def project_exists(db: Session, *, account_id: UUID, project_id: UUID) -> bool:
    """Account-scoped API authorization without a report-time write lock."""
    return _project(db, account_id, project_id, lock=False) is not None


def schedule_repository(
    db: Session, *, account_id: UUID, tracker_id: UUID, repository: str, now: datetime
) -> int:
    """Status events queue only PRs bound to this account and code-host tracker."""
    prs = list(
        db.scalars(
            select(models.IssueCostPullRequest).where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.pr_key.startswith(
                    f"https://bitbucket.org/{repository}/pull-requests/"
                ),
                models.IssueCostPullRequest.merged_at.is_(None),
                models.IssueCostPullRequest.ambiguous.is_(False),
            )
        )
    )
    count = 0
    for pr in prs:
        context = observation_context(db, account_id=account_id, pr_record_id=pr.id)
        if context and context[3].id == tracker_id:
            schedule(db, account_id=account_id, pr_id=pr.id, now=now)
            count += 1
    return count


def report_evidence_many(
    db: Session, *, account_id: UUID, rollups: list[models.IssueCostRollup]
) -> dict[
    UUID,
    tuple[
        TicketCreationEvidence | None,
        list[tuple[ReadinessObservation | None, ReadinessObservation | None]],
        str | None,
        UUID | None,
    ],
]:
    """Batch report evidence so a report does not issue queries per ticket."""
    ids = [row.id for row in rollups]
    if not ids:
        return {}
    tickets = {
        row.rollup_id: TicketCreationEvidence.model_validate(row.evidence)
        for row in db.scalars(
            select(models.TicketCreationRecord).where(
                models.TicketCreationRecord.account_id == account_id,
                models.TicketCreationRecord.rollup_id.in_(ids),
            )
        )
    }
    policies = {
        row.project_id: row.id
        for row in db.scalars(
            select(models.ReadinessPolicyRecord).where(
                models.ReadinessPolicyRecord.account_id == account_id,
                models.ReadinessPolicyRecord.project_id.in_(
                    [r.project_id for r in rollups if r.project_id]
                ),
                models.ReadinessPolicyRecord.is_active.is_(True),
            )
        )
    }
    prs = list(
        db.scalars(
            select(models.IssueCostPullRequest).where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.rollup_id.in_(ids),
            )
        )
    )
    series = {
        (row.pr_id, row.policy_id): row
        for row in db.scalars(
            select(models.ReadinessSeries).where(
                models.ReadinessSeries.account_id == account_id,
                models.ReadinessSeries.pr_id.in_([pr.id for pr in prs]),
                models.ReadinessSeries.policy_id.in_(list(policies.values())),
            )
        )
    }
    observation_ids = {
        identity
        for row in series.values()
        for identity in (row.first_ready_id, row.latest_id)
        if identity
    }
    observations = {
        row.id: ReadinessObservation.model_validate(row.evidence)
        for row in db.scalars(
            select(models.ReadinessObservationRecord).where(
                models.ReadinessObservationRecord.account_id == account_id,
                models.ReadinessObservationRecord.id.in_(observation_ids),
            )
        )
    }
    tracker_types: dict[UUID, str] = {
        identity: kind
        for identity, kind in db.execute(
            select(models.Tracker.id, models.Tracker.tracker_type).where(
                models.Tracker.account_id == account_id,
                models.Tracker.id.in_([r.tracker_id for r in rollups]),
            )
        ).all()
    }
    result = {}
    for rollup in rollups:
        bound = [pr for pr in prs if pr.rollup_id == rollup.id]
        version = policies.get(rollup.project_id) if rollup.project_id else None
        details = []
        if version:
            for pr in bound:
                row = series.get((pr.id, version))
                details.append(
                    (
                        observations.get(row.first_ready_id)
                        if row and row.first_ready_id
                        else None,
                        observations.get(row.latest_id) if row else None,
                    )
                )
        reason = (
            "ambiguous_pr"
            if len(bound) > 1 or any(pr.ambiguous for pr in bound)
            else "unsupported_adapter"
            if tracker_types.get(rollup.tracker_id) != "jira"
            or any(not pr.pr_key.startswith("https://bitbucket.org/") for pr in bound)
            else "policy_unconfigured"
            if not version
            else "missing_binding"
            if not bound
            else None
        )
        result[rollup.id] = tickets.get(rollup.id), details, reason, version
    return result


def get_observation(
    db: Session, *, account_id: UUID, observation_id: UUID
) -> ReadinessObservation | None:
    """Fetch retained per-gate audit evidence by immutable account-scoped identity."""
    row = db.scalar(
        select(models.ReadinessObservationRecord).where(
            models.ReadinessObservationRecord.id == observation_id,
            models.ReadinessObservationRecord.account_id == account_id,
        )
    )
    return ReadinessObservation.model_validate(row.evidence) if row else None
