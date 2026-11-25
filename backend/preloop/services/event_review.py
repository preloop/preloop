"""Admit a read-only review from a signed GitHub pull request event.

The execution stores an :class:`EventReviewBinding` (version 2) in
``ci_review_binding``. There is no CI principal and no CI key. The run
still has no GitHub write credential: the publish step mints its own
lease later, for the base repository only.

Fork pull requests require a server-confirmed maintainer application of
``preloop-review``. Only the base repository is a lease or post target.
Restricted CI admission is unchanged.

The pull request is read with the tracker's GitHub App installation
token, not a downscoped contents lease. GitHub's "Get a pull request"
requires **Pull requests: Read** on that token. A failure to mint the
token is retried (the same as an HTTP 5xx). A 403 or 404 is a refusal.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session, object_session

from preloop.models import crud, models
from preloop.schemas.ci_execution import EventReviewBinding
from preloop.services.publication_credentials import (
    PublicationError,
    validate_publication_tracker,
)

logger = logging.getLogger(__name__)

#: Normalized types that may start a posting run.
EVENT_REVIEW_TYPES: frozenset[str] = frozenset(
    {
        "pull_request_opened",
        "pull_request_updated",
        "pull_request_reopened",
        "pull_request_ready_for_review",
    }
)
#: Raw GitHub actions that may start a posting run. ``edited`` and
#: unrelated label actions are pull request events and do not.
EVENT_REVIEW_ACTIONS: frozenset[str] = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)
FORK_REVIEW_LABEL = "preloop-review"
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_GITHUB_HOSTS = frozenset({"https://github.com", "https://api.github.com"})


class EventReviewUnavailableError(RuntimeError):
    """The provider read could not be completed and should be retried.

    Covers HTTP 5xx, timeouts, and a failure to mint the installation
    token. It is not a refusal of the pull request.
    """

    def __init__(self) -> None:
        super().__init__("Event review provider read unavailable")


class EventReviewDeniedError(Exception):
    """The event is not admitted. ``reason`` is a stable machine code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EventReviewDecision:
    """What ``build_event_review_binding`` decided for one flow.

    Attributes:
        binding: The binding to store, when the run may start as a posting
            review.
        skip_reason: A machine reason that forbids inserting an execution.
            None means the existing event path continues unchanged.
    """

    binding: EventReviewBinding | None
    skip_reason: str | None = None


def event_review_posting_configured(flow: Any) -> bool:
    """Whether the flow is a read-only review that posts from an event.

    Args:
        flow: Flow row or namespace.

    Returns:
        True when posting is on and the trigger types are the event
        allowlist on a tracker source.
    """
    from preloop.services.platform_review import MODE_OFF, platform_review_mode
    from preloop.services.read_only_review import read_only_review

    config = getattr(flow, "git_clone_config", None)
    if not read_only_review(config) or platform_review_mode(config) == MODE_OFF:
        return False
    types = set(getattr(flow, "trigger_event_types", None) or [])
    source = getattr(flow, "trigger_event_source", None)
    if not isinstance(source, str) or source in {"", "webhook", "schedule"}:
        return False
    return bool(types) and types <= EVENT_REVIEW_TYPES


def _github_api_base(tracker: models.Tracker) -> str:
    """Return the public GitHub API, or refuse any other host."""
    url = (getattr(tracker, "url", None) or "https://github.com").rstrip("/")
    if url not in _GITHUB_HOSTS:
        raise EventReviewDeniedError("tracker_unsupported")
    return "https://api.github.com"


async def _installation_token(tracker: models.Tracker) -> str:
    """Mint the tracker's installation token.

    Raises:
        EventReviewUnavailableError: The token could not be minted. Callers
            retry; this is not a refusal.
    """
    installation = getattr(tracker, "oauth_installation", None)
    installation_id = getattr(installation, "external_id", None)
    if installation_id is None:
        raise EventReviewUnavailableError()
    try:
        from preloop.plugins.proprietary.github_app.service import (  # type: ignore[import-untyped]
            get_github_app_service,
        )

        token = await get_github_app_service().get_installation_access_token(
            installation_id
        )
    except Exception:
        raise EventReviewUnavailableError() from None
    if not isinstance(token, str) or not token:
        raise EventReviewUnavailableError()
    return token


async def read_pull_request_for_event(
    tracker: models.Tracker,
    repository_id: str,
    pr_number: int,
) -> dict[str, Any]:
    """Read one pull request with the tracker's installation token.

    The token needs **Pull requests: Read** (GitHub's permission for
    ``GET /repositories/{id}/pulls/{number}``). It is the installation
    access token, not a ``contents: read`` lease. Minting that token is
    retried when it fails. A 403 or 404 refuses the read.

    Args:
        tracker: GitHub App tracker the signed webhook was resolved to.
        repository_id: Provider repository id.
        pr_number: Pull request number.

    Returns:
        The provider's pull request document.

    Raises:
        EventReviewUnavailableError: The token could not be minted, or GitHub
            answered 5xx, 401, 429, or timed out.
        EventReviewDeniedError: GitHub answered 403 or 404, or the host is not
            github.com. ``reason`` is ``provider_denied`` or
            ``tracker_unsupported``.
    """
    base = _github_api_base(tracker)
    token = await _installation_token(tracker)
    repository = quote(str(repository_id), safe="")
    url = f"{base}/repositories/{repository}/pulls/{pr_number}"
    async with httpx.AsyncClient(timeout=25) as client:
        body = await _github_get(client, token, url)
    if not isinstance(body, dict):
        raise EventReviewDeniedError("provider_denied")
    return body


async def _github_get(
    client: httpx.AsyncClient,
    token: str,
    url: str,
    *,
    params: dict[str, int] | None = None,
) -> Any:
    """Read GitHub without redirects; transient failures must be retried."""
    try:
        response = await client.get(
            url,
            params=params,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            follow_redirects=False,
        )
    except (httpx.HTTPError, TimeoutError):
        raise EventReviewUnavailableError() from None
    if response.status_code in {403, 404}:
        raise EventReviewDeniedError("provider_denied")
    if response.status_code != 200:
        raise EventReviewUnavailableError()
    try:
        return response.json()
    except ValueError:
        raise EventReviewUnavailableError() from None


def has_fork_review_label(pull: Mapping[str, Any]) -> bool:
    """Whether GitHub's live pull request still carries the opt-in label."""
    return any(
        isinstance(label, Mapping) and label.get("name") == FORK_REVIEW_LABEL
        for label in (pull.get("labels") or [])
    )


async def read_fork_review_label_application(
    tracker: models.Tracker,
    repository_id: str,
    pr_number: int,
    pull: Mapping[str, Any],
) -> str:
    """Prove the latest label application and its actor's current permission.

    Issue events and collaborator permissions are read from the base repository
    with its tracker's installation token. Webhook sender and author association
    are never authorization evidence. Paginate all events so an older trusted
    application cannot authorize a later untrusted one.

    Returns:
        GitHub's immutable id of the current label application.

    Raises:
        EventReviewDeniedError: Label or maintainer permission is absent.
        EventReviewUnavailableError: A provider read should be retried.
    """
    if not has_fork_review_label(pull):
        raise EventReviewDeniedError("fork_label_missing")
    base = _github_api_base(tracker)
    token = await _installation_token(tracker)
    repository = quote(repository_id, safe="")
    root = f"{base}/repositories/{repository}"
    latest: Mapping[str, Any] | None = None
    latest_id = 0
    async with httpx.AsyncClient(timeout=25) as client:
        page = 1
        while True:
            events = await _github_get(
                client,
                token,
                f"{root}/issues/{pr_number}/events",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(events, list):
                raise EventReviewDeniedError("fork_label_application_missing")
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                label = event.get("label")
                if (
                    event.get("event") not in {"labeled", "unlabeled"}
                    or not isinstance(label, Mapping)
                    or label.get("name") != FORK_REVIEW_LABEL
                ):
                    continue
                identifier = event.get("id")
                if type(identifier) is not int or identifier <= 0:
                    raise EventReviewDeniedError("fork_label_application_missing")
                if identifier > latest_id:
                    latest, latest_id = event, identifier
            if len(events) < 100:
                break
            page += 1
        if latest is None or latest.get("event") != "labeled":
            raise EventReviewDeniedError("fork_label_application_missing")
        actor = latest.get("actor")
        login = actor.get("login") if isinstance(actor, Mapping) else None
        if not isinstance(login, str) or not login:
            raise EventReviewDeniedError("fork_label_actor_not_maintainer")
        permission = await _github_get(
            client,
            token,
            f"{root}/collaborators/{quote(login, safe='')}/permission",
        )
    if not isinstance(permission, Mapping) or permission.get("permission") not in {
        "write",
        "maintain",
        "admin",
    }:
        raise EventReviewDeniedError("fork_label_actor_not_maintainer")
    return str(latest_id)


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _same_repository(repo: Any, identifier: str, slug: str) -> bool:
    """Whether a provider repo object is the bound repository."""
    if not isinstance(repo, Mapping):
        return False
    name = repo.get("full_name")
    return (
        str(repo.get("id")) == identifier
        and isinstance(name, str)
        and name.lower() == slug.lower()
    )


def _skip(reason: str) -> EventReviewDecision:
    return EventReviewDecision(binding=None, skip_reason=reason)


async def build_event_review_binding(
    db: Session, flow: models.Flow, event_data: Mapping[str, Any]
) -> EventReviewDecision:
    """Build the version-2 binding for one flow and one signed event.

    Returns no binding, and does not skip the flow, when posting is off
    or the event is not a pull request event this feature admits. The
    existing path then runs, including today's failure for a read-only
    flow that has not opted in.

    A posting flow whose action is outside the allowlist, whose server
    read disagrees with the webhook, or whose fork lacks a maintainer
    label application, is skipped: ``skip_reason`` is set and no execution is inserted.
    So is an event for a project the flow admits but does not post for
    (``platform_review.project_ids``): ``project_not_in_posting_scope``,
    decided before the provider is read.

    Args:
        db: Database session.
        flow: Flow the event matcher already selected.
        event_data: The normalized event, including ``delivery_id``.

    Returns:
        The binding, or a skip reason, or neither.

    Raises:
        EventReviewUnavailableError: GitHub or the token mint should be retried.
            No execution is inserted.
    """
    if not event_review_posting_configured(flow):
        return EventReviewDecision(binding=None)
    event_type = event_data.get("type")
    if event_type not in EVENT_REVIEW_TYPES:
        return EventReviewDecision(binding=None)
    payload = _mapping(event_data.get("payload")) or {}
    action = payload.get("action")
    labeled = (
        action == "labeled"
        and (_mapping(payload.get("label")) or {}).get("name") == FORK_REVIEW_LABEL
    )
    if action not in EVENT_REVIEW_ACTIONS and not labeled:
        return _skip("action_not_allowlisted")
    if str(getattr(flow, "trigger_event_source", "") or "") != str(
        event_data.get("tracker_id") or ""
    ):
        return EventReviewDecision(binding=None)
    raw_project_ids = getattr(flow, "trigger_project_ids", None)
    project_ids = (
        [str(item) for item in raw_project_ids]
        if isinstance(raw_project_ids, (list, tuple))
        else []
    )
    project_id = event_data.get("project_id")
    if not project_id or str(project_id) not in project_ids:
        return EventReviewDecision(binding=None)
    from preloop.services.platform_review import (
        OUT_OF_SCOPE_REASON,
        project_in_posting_scope,
    )

    if not project_in_posting_scope(flow.git_clone_config, project_id):
        # Admitted to the flow but not posted for: an event-started review
        # exists only as a posting run, so nothing is inserted, not even the
        # failed execution of the older path.
        return _skip(OUT_OF_SCOPE_REASON)
    delivery_id = event_data.get("delivery_id")
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        return _skip("delivery_missing")

    tracker = crud.crud_tracker.get_by_id_and_account(
        db, id=str(flow.trigger_event_source), account_id=str(flow.account_id)
    )
    installation = getattr(tracker, "oauth_installation", None) if tracker else None
    if (
        tracker is None
        or not getattr(tracker, "is_active", False)
        or installation is None
        or getattr(installation, "external_id", None) is None
    ):
        return _skip("tracker_unavailable")
    payload_installation = _mapping(payload.get("installation"))
    if payload_installation is not None and "id" in payload_installation:
        if str(payload_installation.get("id")) != str(installation.external_id):
            return _skip("installation_mismatch")
    if getattr(installation, "suspended_at", None) is not None:
        return _skip("installation_suspended")

    project = crud.crud_project.get(db, id=str(project_id))
    webhook_repo = _mapping(payload.get("repository")) or {}
    webhook_pr = _mapping(payload.get("pull_request")) or {}
    if (
        project is None
        or not project.is_active
        or not project.identifier
        or not project.slug
    ):
        return _skip("project_mismatch")
    number = webhook_pr.get("number")
    if type(number) is not int or number < 1:
        return _skip("pull_request_number_mismatch")
    webhook_sha = None
    webhook_head = _mapping(webhook_pr.get("head"))
    if webhook_head is not None:
        webhook_sha = webhook_head.get("sha")

    installation_id = str(installation.external_id)
    identifier = str(project.identifier)
    slug = str(project.slug)
    project_uuid = project.id
    account_id = UUID(str(flow.account_id))
    flow_id = UUID(str(flow.id))
    tracker_uuid = UUID(str(flow.trigger_event_source))
    tracker_url = getattr(tracker, "url", None)
    if tracker_url is not None and not isinstance(tracker_url, str):
        tracker_url = None
    crud.crud_ci_execution.release_read(db)
    try:
        live = await read_pull_request_for_event(tracker, identifier, number)
    except EventReviewDeniedError as error:
        return _skip(error.reason)

    live_head = _mapping(live.get("head"))
    live_base = _mapping(live.get("base"))
    head_repo = _mapping(live_head.get("repo")) if live_head else None
    base_repo = _mapping(live_base.get("repo")) if live_base else None
    if not _same_repository(base_repo, identifier, slug):
        return _skip("repository_mismatch")
    live_slug = base_repo.get("full_name") if base_repo is not None else None
    if not isinstance(live_slug, str):
        return _skip("repository_mismatch")
    is_fork = not _same_repository(head_repo, identifier, slug)
    if labeled and not is_fork:
        return _skip("action_not_allowlisted")
    if is_fork and not has_fork_review_label(live):
        return _skip("fork_pull_request")
    if str(webhook_repo.get("id")) != identifier or (
        not isinstance(webhook_repo.get("full_name"), str)
        or webhook_repo["full_name"].lower() != slug.lower()
    ):
        return _skip("repository_mismatch")
    if live.get("number") != number:
        return _skip("pull_request_number_mismatch")
    provider_id = live.get("id")
    webhook_id = webhook_pr.get("id")
    if (
        type(provider_id) not in (str, int)
        or not str(provider_id)
        or str(webhook_id) != str(provider_id)
    ):
        return _skip("pull_request_id_mismatch")
    live_sha = live_head.get("sha") if live_head else None
    if (
        not isinstance(live_sha, str)
        or not _SHA_RE.fullmatch(live_sha)
        or live_sha != webhook_sha
    ):
        return _skip("head_sha_mismatch")
    base_sha = live_base.get("sha") if live_base else None
    base_ref = live_base.get("ref") if live_base else None
    if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
        return _skip("base_sha_mismatch")
    if not isinstance(base_ref, str):
        return _skip("base_branch_mismatch")
    draft = live.get("draft")
    if type(draft) is not bool:
        return _skip("binding_invalid")
    fork_label_event_id = None
    if is_fork:
        if live.get("state") != "open":
            return _skip("pull_request_closed")
        try:
            fork_label_event_id = await read_fork_review_label_application(
                tracker, identifier, number, live
            )
        except EventReviewDeniedError as error:
            return _skip(error.reason)
    try:
        binding = EventReviewBinding(
            version=2,
            source="tracker_event",
            account_id=account_id,
            flow_id=flow_id,
            tracker_id=tracker_uuid,
            installation_id=installation_id,
            project_id=project_uuid,
            repository_identifier=identifier,
            repository_slug=live_slug,
            tracker_type="github",
            tracker_url=tracker_url,
            provider_pr_id=str(provider_id),
            pr_number=number,
            head_sha=live_sha,
            base_sha=base_sha,
            base_branch=base_ref,
            event_type=event_type,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            delivery_id=delivery_id,
            draft=draft,
            fork_label_event_id=fork_label_event_id,
        )
    except (ValidationError, ValueError):
        return _skip("binding_invalid")
    return EventReviewDecision(binding=binding)


def authorize_event_review(
    db: Session, execution: models.FlowExecution
) -> EventReviewBinding:
    """Re-check that an event review may still be posted.

    Args:
        db: Database session.
        execution: Execution whose binding is version 2.

    Returns:
        The parsed binding.

    Raises:
        PlatformReviewError: Admission no longer holds. ``reason`` is one
            of ``admission_revoked``, ``binding_changed``,
            ``tracker_unsupported`` or ``installation_suspended``.
    """
    from preloop.services.platform_review import (
        PlatformReviewError,
        project_in_posting_scope,
    )

    try:
        binding = EventReviewBinding.model_validate(execution.ci_review_binding)
    except ValidationError:
        raise PlatformReviewError("admission_revoked") from None
    if (
        execution.ci_principal_id is not None
        or execution.initiating_ci_key_id is not None
        or execution.flow_id != binding.flow_id
        or execution.parent_execution_id is not None
        or execution.root_execution_id is not None
        or execution.batch_id is not None
        or execution.retry_of_execution_id is not None
        or (execution.delegation_depth or 0) != 0
    ):
        raise PlatformReviewError("admission_revoked")
    flow = crud.crud_flow.get(db, id=binding.flow_id, account_id=binding.account_id)
    if (
        flow is None
        or not flow.is_enabled
        or str(flow.account_id) != str(binding.account_id)
        or str(flow.trigger_event_source or "") != str(binding.tracker_id)
        or binding.event_type not in list(flow.trigger_event_types or [])
        or str(binding.project_id)
        not in [str(item) for item in (flow.trigger_project_ids or [])]
        or not project_in_posting_scope(flow.git_clone_config, binding.project_id)
    ):
        raise PlatformReviewError("admission_revoked")
    repositories = [
        repo
        for repo in (flow.git_clone_config or {}).get("repositories") or []
        if isinstance(repo, Mapping)
        and repo.get("project_id") == str(binding.project_id)
    ]
    if (
        len(repositories) != 1
        or repositories[0].get("tracker_id") != str(binding.tracker_id)
        or repositories[0].get("repository_url")
    ):
        raise PlatformReviewError("binding_changed")
    project = crud.crud_project.get(db, id=binding.project_id)
    if (
        project is None
        or not project.is_active
        or str(project.identifier) != binding.repository_identifier
        or (project.slug or "").lower() != binding.repository_slug.lower()
    ):
        raise PlatformReviewError("binding_changed")
    tracker = crud.crud_tracker.get_by_id_and_account(
        db, id=str(binding.tracker_id), account_id=str(binding.account_id)
    )
    installation = getattr(tracker, "oauth_installation", None) if tracker else None
    try:
        if tracker is None or not tracker.is_active:
            raise PublicationError("missing")
        validate_publication_tracker(tracker)
        if (
            installation is None
            or str(installation.external_id) != binding.installation_id
        ):
            raise PublicationError("installation")
    except PublicationError:
        raise PlatformReviewError("tracker_unsupported") from None
    if getattr(installation, "suspended_at", None) is not None:
        raise PlatformReviewError("installation_suspended", retryable=False)
    return binding


def _live_matches(binding: EventReviewBinding, pull: Mapping[str, Any]) -> bool:
    """Whether the live pull request is still the admitted one."""
    head = _mapping(pull.get("head"))
    base = _mapping(pull.get("base"))
    base_repo = _mapping(base.get("repo")) if base else None
    if not _same_repository(
        base_repo, binding.repository_identifier, binding.repository_slug
    ):
        return False
    head_repo = _mapping(head.get("repo")) if head else None
    if binding.fork_label_event_id is None and not _same_repository(
        head_repo, binding.repository_identifier, binding.repository_slug
    ):
        return False
    if binding.fork_label_event_id is not None and (
        pull.get("state") != "open" or not has_fork_review_label(pull)
    ):
        return False
    # The base SHA may advance on the same branch. A different base ref
    # means the pull request was retargeted, so the stored base is stale.
    base_ref = base.get("ref") if isinstance(base, Mapping) else None
    return (
        str(pull.get("id")) == binding.provider_pr_id
        and pull.get("number") == binding.pr_number
        and isinstance(head, Mapping)
        and head.get("sha") == binding.head_sha
        and isinstance(base_ref, str)
        and base_ref == binding.base_branch
    )


async def ensure_event_review_admission(
    db: Session, *, execution: models.FlowExecution
) -> None:
    """Re-read the pull request before an event review is launched.

    A mismatch, a 403 or a 404 raises :class:`EventReviewDeniedError`. The
    caller fails the row. A 5xx or a token-mint failure raises
    :class:`EventReviewUnavailableError` and the row stays pending.

    Args:
        db: Database session.
        execution: Execution that may carry a version-2 binding.

    Raises:
        EventReviewDeniedError: The run is no longer admitted.
        EventReviewUnavailableError: The provider read should be retried.
    """
    if (
        execution.ci_principal_id is not None
        or execution.initiating_ci_key_id is not None
    ):
        return
    try:
        binding = EventReviewBinding.model_validate(execution.ci_review_binding)
    except ValidationError:
        return
    from preloop.services.platform_review import PlatformReviewError

    try:
        authorize_event_review(db, execution)
    except PlatformReviewError as error:
        raise EventReviewDeniedError(error.reason) from None
    tracker = crud.crud_tracker.get_by_id_and_account(
        db, id=str(binding.tracker_id), account_id=str(binding.account_id)
    )
    if tracker is None:
        raise EventReviewDeniedError("tracker_unsupported")
    crud.crud_ci_execution.release_read(db)
    pull = await read_pull_request_for_event(
        tracker, binding.repository_identifier, binding.pr_number
    )
    if not _live_matches(binding, pull):
        raise EventReviewDeniedError("provider_disagrees")
    if binding.fork_label_event_id is not None:
        application = await read_fork_review_label_application(
            tracker, binding.repository_identifier, binding.pr_number, pull
        )
        if application != binding.fork_label_event_id:
            raise EventReviewDeniedError("fork_label_application_changed")


def _config_mapping(config: Any) -> dict[str, Any] | None:
    if hasattr(config, "model_dump"):
        dumped = config.model_dump()
        return dumped if isinstance(dumped, dict) else None
    if isinstance(config, Mapping):
        return dict(config)
    return None


def _webhook_mapping(webhook_config: Any) -> dict[str, Any]:
    mapped = _config_mapping(webhook_config)
    return mapped or {}


def validate_event_review_posting(
    db: Session,
    account_id: Any,
    config: Any,
    trigger_event_source: Any,
    trigger_event_types: Any,
    trigger_project_ids: Any,
    webhook_config: Any,
) -> None:
    """Refuse an event-started posting flow that is not the admitted shape.

    Does nothing when posting is off, or when the trigger types are empty
    or only ``webhook`` or ``schedule``. Those flows stay on the CI checks.

    Args:
        db: Database session.
        account_id: Account that owns the flow.
        config: ``git_clone_config`` the flow will have after the save.
        trigger_event_source: Trigger source after the save.
        trigger_event_types: Trigger types after the save.
        trigger_project_ids: Project ids after the save.
        webhook_config: Webhook config after the save.

    Raises:
        ValueError: The combination cannot start a safe posting run.
    """
    from preloop.services.platform_review import platform_review_block

    stored = _config_mapping(config)
    if platform_review_block(stored) is None:
        return
    types = list(trigger_event_types or [])
    if not types or set(types) <= {"webhook", "schedule"}:
        return
    outside = [name for name in types if name not in EVENT_REVIEW_TYPES]
    if outside or len(types) != len(set(types)):
        raise ValueError(
            "An event-started review only runs on pull_request_opened, "
            "pull_request_updated, pull_request_reopened or "
            "pull_request_ready_for_review."
        )
    try:
        tracker_id = UUID(str(trigger_event_source))
    except (TypeError, ValueError):
        raise ValueError(
            "An event-started review needs its GitHub App tracker as the "
            "trigger source."
        ) from None
    tracker = crud.crud_tracker.get(db, id=tracker_id, account_id=str(account_id))
    try:
        if tracker is None:
            raise PublicationError("missing")
        validate_publication_tracker(tracker)
    except PublicationError:
        raise ValueError(
            "An event-started review needs a GitHub App tracker. A personal "
            "access token, GitLab or Bitbucket tracker cannot post."
        ) from None
    project_ids = [str(item) for item in (trigger_project_ids or [])]
    if not project_ids or len(project_ids) != len(set(project_ids)):
        raise ValueError(
            "An event-started review needs each project listed once in "
            "trigger_project_ids."
        )
    repositories = (stored or {}).get("repositories") or []
    by_project: dict[str, list[Mapping[str, Any]]] = {}
    for repo in repositories:
        if isinstance(repo, Mapping) and repo.get("project_id"):
            by_project.setdefault(str(repo["project_id"]), []).append(repo)
    for project_id in project_ids:
        matches = by_project.get(project_id) or []
        if (
            len(matches) != 1
            or matches[0].get("tracker_id") != str(tracker_id)
            or matches[0].get("repository_url")
        ):
            raise ValueError(
                "An event-started review needs each project on that GitHub "
                "App tracker, with no repository URL."
            )
    if _webhook_mapping(webhook_config).get("supersede_on_update") is not True:
        raise ValueError(
            "An event-started review needs webhook_config.supersede_on_update "
            "set to true, so a new commit is reviewed instead of being dropped "
            "behind the run already in progress."
        )


def flow_has_ci_identity(flow: Any) -> bool:
    """Whether the flow still has a CI identity that can start it.

    Args:
        flow: Flow row.

    Returns:
        True when a principal row still names this flow.
    """
    session = object_session(flow)
    flow_id = getattr(flow, "id", None)
    if session is None or flow_id is None:
        return False
    return (
        session.query(models.CiPrincipal.id)
        .filter(models.CiPrincipal.flow_id == flow_id)
        .first()
        is not None
    )
