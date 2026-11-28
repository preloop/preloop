"""Deterministic pre-check stage for flows (#2541).

A flow's pre-check is evaluated on the server before an execution is
dispatched, and decides one of three things:

* **skip**: no check matches. The caller records a ``SKIPPED`` execution and
  stops: no worker claim, no runner lease, no owner consent, no model call.
* **run**: something matches (or ``max_skip_hours`` forces a run). The
  decision document is passed to the run at
  ``trigger_event_details["payload"]["precheck"]``, so a prompt reads the
  matched items with ``{{trigger_event.payload.precheck}}``.
* **fail**: the tracker could not answer. The caller records a ``FAILED``
  execution (``precheck_failed``), unless ``on_error: run`` asks to run with
  the error named in the context. A stored pre-check that no longer
  validates always fails, as ``flow_config``.

Checks are data, not code: each built-in check is a pure function over
read-only tracker reads (``flow_precheck_reader``) and a few facts the engine
reads from Preloop's own execution rows through ``preloop.models.crud``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
)

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.schemas.flow_precheck import FlowPrecheck, OpenPullRequestsCheck
from preloop.services.flow_precheck_reader import (
    CAUSE_ENGINE_ERROR,
    CAUSE_TIMEOUT,
    CAUSE_UNSUPPORTED,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_TIMEOUT_SECONDS,
    SUPPORTED_TRACKER_TYPES,
    PrecheckBudget,
    PrecheckError,
    PrecheckTrackerReader,
    build_reader,
)

logger = logging.getLogger(__name__)

#: Largest decision document passed to a run, in bytes of JSON.
MAX_CONTEXT_BYTES = 64 * 1024
#: Room kept free under the cap for the keys written after capping (the
#: digest, the decision, a skip's fold count and timestamps).
_RESERVED_BYTES = 1024
#: Longest tracker title copied into the decision document.
MAX_TITLE_CHARS = 120

#: Skip reasons. ``unchanged`` and ``held`` are produced by later features
#: that build on this engine.
SKIP_NOTHING_TO_DO = "nothing_to_do"

#: Trigger kinds the engine is called for.
TriggerKind = Literal["schedule", "manual", "dry_run"]

#: Tracker types whose projects are code repositories. A Jira project on a
#: flow names issues, not a repository, so it is not a pre-check target.
_REPOSITORY_TRACKER_TYPES = frozenset(
    {"github", "gitlab", "bitbucket", "bitbucket_dc", "azure_devops"}
)
_GITHUB_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_number(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def precheck_enabled() -> bool:
    """``PRELOOP_FLOW_PRECHECK_ENABLED``, default true.

    False ignores every stored pre-check, so each tick runs as it did before
    the feature existed. The kill switch for this stage.
    """
    return _env_bool("PRELOOP_FLOW_PRECHECK_ENABLED", True)


def precheck_timeout_seconds() -> float:
    """``PRELOOP_PRECHECK_TIMEOUT_SECONDS``, default 20."""
    return _env_number(
        "PRELOOP_PRECHECK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=1.0
    )


def precheck_max_requests() -> int:
    """``PRELOOP_PRECHECK_MAX_REQUESTS``, default 30."""
    return int(
        _env_number("PRELOOP_PRECHECK_MAX_REQUESTS", DEFAULT_MAX_REQUESTS, minimum=1)
    )


class PrecheckConfigError(ValueError):
    """The pre-check cannot run as configured for this flow."""


@dataclass(frozen=True)
class PrecheckRepository:
    """A repository a pre-check may read, with the tracker that serves it."""

    slug: str
    tracker_type: str
    tracker: Any = field(compare=False, repr=False)

    @property
    def key(self) -> str:
        """Case-insensitive identity of the repository."""
        return self.slug.lower()


@dataclass
class CheckResult:
    """What one check found."""

    matched: bool
    count: int
    items: List[Dict[str, Any]]
    truncated: bool
    summary: str

    def as_document(self, check_type: str) -> Dict[str, Any]:
        """The check's entry in the decision document."""
        return {
            "type": check_type,
            "matched": self.matched,
            "count": self.count,
            "truncated": self.truncated,
            "summary": self.summary,
            "items": self.items,
        }


@dataclass
class CheckContext:
    """Everything a check may use; checks do not touch the database."""

    repositories: List[PrecheckRepository]
    readers: Callable[[PrecheckRepository], Awaitable[PrecheckTrackerReader]]
    now: datetime
    #: ``scheduled_at`` (else start) of the newest non-skipped execution, or
    #: the flow's creation time.
    last_run_at: datetime
    #: The schedule's previous fire time, when the trigger carries one.
    previous_tick_at: Optional[datetime]
    #: Normalized URLs of pull requests a live execution is bound to.
    active_pull_request_urls: Set[str]


CheckFunction = Callable[[Any, CheckContext], Awaitable[CheckResult]]

#: Built-in checks, ``type -> evaluate``. Filled by
#: ``preloop.services.flow_precheck_checks``; :func:`register_check` adds more.
CHECKS: Dict[str, CheckFunction] = {}


def register_check(check_type: str, evaluate: CheckFunction) -> None:
    """Register the evaluate function of a check type.

    Args:
        check_type: The ``type`` discriminator of the check.
        evaluate: ``async (params, context) -> CheckResult``.
    """
    CHECKS[check_type] = evaluate


@dataclass
class PrecheckOutcome:
    """The engine's decision, ready for the caller to record or run."""

    action: Literal["run", "skip", "fail"]
    document: Dict[str, Any]
    summary: str
    reason: Optional[str] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None


def truncate_title(value: Any) -> str:
    """A tracker title cut to :data:`MAX_TITLE_CHARS` characters."""
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def iso(value: Optional[datetime]) -> Optional[str]:
    """ISO 8601 in UTC with a ``Z`` suffix, or None."""
    if value is None:
        return None
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC and return an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


# -- repository scope ---------------------------------------------------------


def _project_ids(trigger_project_ids: Any, git_clone_config: Any) -> List[str]:
    ids: List[str] = []
    for value in trigger_project_ids or []:
        if value:
            ids.append(str(value))
    config = _as_mapping(git_clone_config)
    for entry in config.get("repositories") or []:
        entry = _as_mapping(entry)
        if entry.get("project_id"):
            ids.append(str(entry["project_id"]))
    return list(dict.fromkeys(ids))


def _as_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        dumped: Dict[str, Any] = value.model_dump()
        return dumped
    return dict(value) if isinstance(value, Mapping) else {}


def resolve_flow_repositories(
    db: Session,
    *,
    account_id: Any,
    trigger_project_ids: Any,
    git_clone_config: Any,
) -> List[PrecheckRepository]:
    """Repositories of the flow's own projects, the only ones a check may read.

    The flow's projects are its ``trigger_project_ids`` and the projects or
    repository URLs in ``git_clone_config.repositories``. Projects of trackers
    that do not host code (Jira) are left out.

    Args:
        db: Database session.
        account_id: The flow's account; every lookup is scoped to it.
        trigger_project_ids: The flow's trigger projects.
        git_clone_config: The flow's clone configuration.

    Returns:
        Repositories in configuration order, without duplicates.
    """
    from preloop.models.crud import crud_project, crud_tracker

    found: Dict[str, PrecheckRepository] = {}
    if not account_id:
        # The CRUD lookups only scope by a truthy account; a flow without one
        # (a global preset) has no projects of its own to read.
        return []

    def add(slug: str, tracker: Any) -> None:
        tracker_type = str(getattr(tracker, "tracker_type", "") or "")
        if not slug or tracker_type not in _REPOSITORY_TRACKER_TYPES:
            return
        repo = PrecheckRepository(slug=slug, tracker_type=tracker_type, tracker=tracker)
        found.setdefault(repo.key, repo)

    for project_id in _project_ids(trigger_project_ids, git_clone_config):
        project = crud_project.get(db, id=project_id, account_id=account_id)
        organization = getattr(project, "organization", None) if project else None
        tracker = getattr(organization, "tracker", None)
        if project is None or tracker is None:
            continue
        add(str(project.slug or project.identifier or ""), tracker)

    config = _as_mapping(git_clone_config)
    for entry in config.get("repositories") or []:
        entry = _as_mapping(entry)
        if entry.get("project_id") or not entry.get("tracker_id"):
            continue
        match = _GITHUB_URL.match(str(entry.get("repository_url") or "").strip())
        if not match:
            continue
        tracker = crud_tracker.get(db, id=entry["tracker_id"], account_id=account_id)
        if tracker is not None:
            add(f"{match.group(1)}/{match.group(2)}", tracker)
    return list(found.values())


def repositories_for_check(
    check: Any, available: Sequence[PrecheckRepository]
) -> List[PrecheckRepository]:
    """The repositories one check reads, checked against the flow's scope.

    Raises:
        PrecheckConfigError: A named repository is outside the flow's
            projects, the check has nothing to read, or a repository's
            tracker has no pre-check reader.
    """
    by_key = {repo.key: repo for repo in available}
    if check.repositories:
        chosen = []
        for name in check.repositories:
            repo = by_key.get(name.strip().lower())
            if repo is None:
                raise PrecheckConfigError(
                    f"pre-check '{check.id}' names repository '{name}', which is "
                    "not one of the flow's projects"
                )
            chosen.append(repo)
    else:
        chosen = list(available)
    if not chosen:
        raise PrecheckConfigError(
            f"pre-check '{check.id}' has no repository to read: give the flow a "
            "project or a repository in git_clone_config"
        )
    for repo in chosen:
        if repo.tracker_type not in SUPPORTED_TRACKER_TYPES:
            raise PrecheckConfigError(
                f"pre-check '{check.id}' cannot read '{repo.slug}': "
                f"{repo.tracker_type} trackers are not supported yet"
            )
    return list(dict.fromkeys(chosen))


def validate_precheck_scope(
    db: Session,
    *,
    account_id: Any,
    precheck: Any,
    trigger_project_ids: Any,
    git_clone_config: Any,
) -> None:
    """Refuse a pre-check that reads outside the flow's projects.

    Called on save, so the console and API answer 422 before the flow
    exists, and again at evaluation.

    Args:
        db: Database session.
        account_id: The flow's account.
        precheck: ``FlowPrecheck`` or its stored mapping; None passes.
        trigger_project_ids: The flow's trigger projects after the save.
        git_clone_config: The flow's clone configuration after the save.

    Raises:
        PrecheckConfigError: With a message naming the check and repository.
    """
    if precheck is None:
        return
    parsed = (
        precheck
        if isinstance(precheck, FlowPrecheck)
        else FlowPrecheck.model_validate(precheck)
    )
    available = resolve_flow_repositories(
        db,
        account_id=account_id,
        trigger_project_ids=trigger_project_ids,
        git_clone_config=git_clone_config,
    )
    for check in parsed.checks:
        repositories_for_check(check, available)


# -- the decision document ----------------------------------------------------


def _item_identity(item: Mapping[str, Any]) -> Dict[str, Any]:
    """The fields of an item that say whether the work changed."""
    keys = ("repository", "number", "id", "head_sha", "reasons", "failing_checks")
    return {key: item[key] for key in keys if key in item}


def compute_digest(checks: Mapping[str, Mapping[str, Any]]) -> str:
    """A stable digest over the matched items of every check.

    Independent of titles and timestamps: the same pull requests at the same
    heads with the same reasons give the same digest.
    """
    material = {
        check_id: sorted(
            (
                json.dumps(_item_identity(item), sort_keys=True, default=str)
                for item in entry.get("items") or []
            )
        )
        for check_id, entry in sorted(checks.items())
    }
    raw = json.dumps(material, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def cap_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Drop items from the end until the document fits :data:`MAX_CONTEXT_BYTES`.

    Items are dropped from the check with the most items first; every check
    that lost an item is marked ``truncated``.
    """

    def size() -> int:
        return len(json.dumps(document, default=str).encode("utf-8"))

    checks: Dict[str, Dict[str, Any]] = document.get("checks") or {}
    while size() > MAX_CONTEXT_BYTES - _RESERVED_BYTES:
        largest: Dict[str, Any] = max(
            checks.values(), key=lambda entry: len(entry.get("items") or []), default={}
        )
        items = largest.get("items") or []
        if not items:
            break
        items.pop()
        largest["truncated"] = True
    return document


def _summary(results: Mapping[str, Mapping[str, Any]]) -> str:
    parts = [str(entry.get("summary") or "") for entry in results.values()]
    text = "; ".join(part for part in parts if part)
    return text[:1000] if text else "Pre-check evaluated"


# -- evaluation ---------------------------------------------------------------


@dataclass
class _InternalFacts:
    last_run_at: datetime
    last_real_run_at: Optional[datetime]
    active_pull_request_urls: Set[str]


def _read_internal_facts(
    db: Session, flow: Any, *, needs_active: bool
) -> _InternalFacts:
    """Facts from Preloop's own rows, read through the CRUD layer."""
    from preloop.models.crud import crud_flow_execution
    from preloop.services.flow_pr_binding import normalize_pr_url

    created = _parse_iso(getattr(flow, "created_at", None)) or datetime.now(
        timezone.utc
    )
    last = crud_flow_execution.last_non_skipped(db, flow_id=flow.id)
    last_real: Optional[datetime] = None
    last_run_at = created
    if last is not None:
        last_real = _parse_iso(last.start_time)
        details: Dict[str, Any] = dict(last.trigger_event_details or {})
        payload: Dict[str, Any] = details.get("payload") or {}
        scheduled = _parse_iso(payload.get("scheduled_at")) if payload else None
        last_run_at = scheduled or last_real or created
    active: Set[str] = set()
    if needs_active and getattr(flow, "account_id", None) is not None:
        for url in crud_flow_execution.active_pull_request_urls(
            db, account_id=flow.account_id
        ):
            normalized = normalize_pr_url(url)
            if normalized:
                active.add(normalized)
    return _InternalFacts(
        last_run_at=last_run_at,
        last_real_run_at=last_real,
        active_pull_request_urls=active,
    )


def _needs_active_executions(precheck: FlowPrecheck) -> bool:
    return any(
        isinstance(check, OpenPullRequestsCheck) and check.exclude.active_execution
        for check in precheck.checks
    )


def _ensure_checks_loaded() -> None:
    if not CHECKS:
        import preloop.services.flow_precheck_checks  # noqa: F401


async def _off_loop(operation: Callable[[], Any]) -> Any:
    from preloop.api.loop_safety import run_db_off_loop

    return await run_db_off_loop(operation)


ReaderFactory = Callable[[Any, PrecheckBudget], Awaitable[PrecheckTrackerReader]]


async def evaluate_precheck(
    db: Session,
    flow: Any,
    event_data: Optional[Mapping[str, Any]],
    *,
    trigger_kind: TriggerKind,
    precheck: Any = None,
    reader_factory: Optional[ReaderFactory] = None,
    now: Optional[datetime] = None,
) -> Optional[PrecheckOutcome]:
    """Evaluate the flow's pre-check for one trigger.

    Args:
        db: Database session, used off the event loop through the CRUD layer.
        flow: The flow row.
        event_data: The trigger about to start a run; a schedule trigger
            carries ``payload.previous_scheduled_at``.
        trigger_kind: ``schedule`` or ``manual`` evaluate only when the
            pre-check lists them in ``applies_to``; ``dry_run`` always does.
        precheck: A draft to evaluate instead of the stored one (dry run).
        reader_factory: Builds a reader for a tracker; defaults to
            :func:`~preloop.services.flow_precheck_reader.build_reader`.
        now: Clock override for tests.

    Returns:
        None when no pre-check applies (none configured, the feature is
        switched off, or the trigger kind is not gated); otherwise the
        outcome. Never raises for a tracker or engine failure: those are
        outcomes too.
    """
    raw = precheck if precheck is not None else getattr(flow, "precheck", None)
    if raw is None:
        return None
    if trigger_kind != "dry_run" and not precheck_enabled():
        return None
    moment = as_utc(now or datetime.now(timezone.utc))
    try:
        parsed = (
            raw if isinstance(raw, FlowPrecheck) else FlowPrecheck.model_validate(raw)
        )
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
        reason = errors[0]["msg"] if errors else "invalid"
        message = (
            "stored pre-check is not valid"
            + (f" at {where}" if where else "")
            + f": {reason}"
        )
        return _config_failure(moment, message)
    if trigger_kind != "dry_run" and trigger_kind not in parsed.applies_to:
        return None

    _ensure_checks_loaded()
    try:
        available, facts = await _off_loop(
            lambda: (
                resolve_flow_repositories(
                    db,
                    account_id=flow.account_id,
                    trigger_project_ids=flow.trigger_project_ids,
                    git_clone_config=flow.git_clone_config,
                ),
                _read_internal_facts(
                    db, flow, needs_active=_needs_active_executions(parsed)
                ),
            )
        )
        plans = [
            (check, repositories_for_check(check, available)) for check in parsed.checks
        ]
    except PrecheckConfigError as exc:
        return _config_failure(moment, str(exc))

    payload = (event_data or {}).get("payload") if event_data else None
    previous_tick = _parse_iso((payload or {}).get("previous_scheduled_at"))
    budget = PrecheckBudget(
        max_requests=precheck_max_requests(),
        timeout_seconds=precheck_timeout_seconds(),
    )
    readers: Dict[str, PrecheckTrackerReader] = {}
    factory = reader_factory or build_reader

    async def reader_for(repo: PrecheckRepository) -> PrecheckTrackerReader:
        key = str(getattr(repo.tracker, "id", repo.key))
        if key not in readers:
            readers[key] = await factory(repo.tracker, budget)
        return readers[key]

    results: Dict[str, Dict[str, Any]] = {}
    error: Optional[PrecheckError] = None

    async def run_all() -> None:
        for check, repositories in plans:
            evaluate = CHECKS.get(check.type)
            if evaluate is None:
                raise PrecheckError(
                    CAUSE_UNSUPPORTED,
                    f"check type '{check.type}' is not available",
                    check=check.id,
                )
            context = CheckContext(
                repositories=repositories,
                readers=reader_for,
                now=moment,
                last_run_at=facts.last_run_at,
                previous_tick_at=previous_tick,
                active_pull_request_urls=facts.active_pull_request_urls,
            )
            try:
                result = await evaluate(check, context)
            except PrecheckError as exc:
                exc.check = exc.check or check.id
                raise
            results[check.id] = result.as_document(check.type)

    current = parsed.checks[0].id
    try:
        await asyncio.wait_for(run_all(), timeout=budget.remaining_seconds())
    except PrecheckError as exc:
        error = exc
    except asyncio.TimeoutError:
        pending = [c.id for c, _ in plans if c.id not in results]
        error = PrecheckError(
            CAUSE_TIMEOUT,
            "pre-check ran out of time",
            check=pending[0] if pending else current,
        )
    except Exception as exc:  # noqa: BLE001 - an engine bug fails visibly
        logger.exception("Pre-check engine error for flow %s", flow.id)
        pending = [c.id for c, _ in plans if c.id not in results]
        error = PrecheckError(
            CAUSE_ENGINE_ERROR,
            f"pre-check failed unexpectedly ({type(exc).__name__})",
            check=pending[0] if pending else current,
        )

    document: Dict[str, Any] = {
        "version": 1,
        "evaluated_at": iso(moment),
        "run_if": parsed.run_if,
        "forced": False,
        "requests": budget.requests,
        "checks": results,
    }
    if error is not None:
        document["precheck_error"] = {"check": error.check, "cause": error.cause}
        message = f"pre-check '{error.check}' failed: {error.cause}: {error.message}"
        if parsed.on_error == "run":
            document["decision"] = "run"
            cap_document(document)
            document["digest"] = compute_digest(document["checks"])
            return PrecheckOutcome(
                action="run", document=document, summary=message[:1000]
            )
        document["decision"] = "fail"
        from preloop.services.flow_failure_category import (
            FAILURE_CATEGORY_PRECHECK_FAILED,
        )

        return PrecheckOutcome(
            action="fail",
            document=document,
            summary=message[:1000],
            failure_category=FAILURE_CATEGORY_PRECHECK_FAILED,
            error_message=message[:2000],
        )

    matched = [entry["matched"] for entry in results.values()]
    should_run = any(matched) if parsed.run_if == "any" else all(matched)
    forced = False
    if not should_run and parsed.max_skip_hours is not None:
        reference = facts.last_real_run_at or _parse_iso(
            getattr(flow, "created_at", None)
        )
        if reference is not None and moment - reference >= timedelta(
            hours=parsed.max_skip_hours
        ):
            forced = True
    cap_document(document)
    document["digest"] = compute_digest(results)
    summary = _summary(results)
    if should_run or forced:
        document["decision"] = "run"
        document["forced"] = forced
        return PrecheckOutcome(action="run", document=document, summary=summary)
    document["decision"] = "skip"
    document["reason"] = SKIP_NOTHING_TO_DO
    return PrecheckOutcome(
        action="skip", document=document, summary=summary, reason=SKIP_NOTHING_TO_DO
    )


def _config_failure(moment: datetime, message: str) -> PrecheckOutcome:
    from preloop.services.flow_failure_category import FAILURE_CATEGORY_FLOW_CONFIG

    return PrecheckOutcome(
        action="fail",
        document={
            "version": 1,
            "evaluated_at": iso(moment),
            "decision": "fail",
            "precheck_error": {"check": None, "cause": "flow_config"},
            "checks": {},
        },
        summary=message[:1000],
        failure_category=FAILURE_CATEGORY_FLOW_CONFIG,
        error_message=message[:2000],
    )


def with_precheck_document(
    event_data: Mapping[str, Any], document: Mapping[str, Any]
) -> Dict[str, Any]:
    """A copy of ``event_data`` with the decision at ``payload.precheck``."""
    copied = dict(event_data)
    payload = copied.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    payload["precheck"] = dict(document)
    copied["payload"] = payload
    return copied
