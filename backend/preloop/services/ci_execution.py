"""Trusted provider admission and narrow projections for CI review executions."""

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import UUID

import gitlab
import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.models import crud, models
from preloop.models.crud.ci_execution import context_from_binding
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.schemas.ci_execution import (
    CiReviewBinding,
    CiReviewRequest,
    ci_review_event,
)
from preloop.schemas.ci_principal import CiAction
from preloop.services.flow_feedback import feedback_policy
from preloop.services.flow_feedback_provider import feedback_tracker_options
from preloop.services.issue_triage_controller import is_triage_flow
from preloop.services.kill_switch import FlowHaltActiveError, flows_halted
from preloop.services.model_routing import prepare_execution_routing
from preloop.sync.event_normalizer import attach_trigger_subject
from preloop.sync.exceptions import TrackerError, TrackerResponseError
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.gitlab import GitLabTracker


class CiReviewDeniedError(PermissionError):
    """Inputs or current authority cannot admit a restricted review."""


class CiReviewUnavailableError(RuntimeError):
    """Trusted provider verification or execution dispatch is unavailable."""

    def __init__(self, execution_id: UUID | None = None) -> None:
        super().__init__("Restricted CI review verification unavailable")
        self.execution_id = execution_id


@dataclass(frozen=True)
class _ProviderRead:
    context: CiAuthorizationContext
    request: CiReviewRequest
    tracker_key: str = field(repr=False)
    tracker_options: dict[str, Any] = field(repr=False)


def _review_flow(db: Session, context: CiAuthorizationContext) -> models.Flow:
    flow = crud.crud_flow.get(db, id=context.flow_id, account_id=context.account_id)
    if (
        flow is None
        or is_triage_flow(db, flow)
        or flow.callable_flows
        or feedback_policy(flow)
    ):
        raise CiReviewDeniedError("Restricted CI requires a single review execution")
    if flows_halted(db, context.account_id):
        raise FlowHaltActiveError("Account flow executions are halted")
    return flow


def _provider_url(provider: str, raw_url: str | None) -> str:
    """Normalize only conventional endpoints; never silently switch hosts."""
    if provider == "github":
        url = (raw_url or "https://github.com").rstrip("/")
        if url not in {"https://github.com", "https://api.github.com"}:
            raise CiReviewDeniedError("Restricted CI GitHub host unsupported")
        return "https://github.com"
    url = (raw_url or "https://gitlab.com").rstrip("/")
    return url[:-7] if url.endswith("/api/v4") else url


def _bound_provider_options(
    context: CiAuthorizationContext,
    options: dict[str, Any],
) -> dict[str, Any]:
    expected = _provider_url(context.tracker_type, context.tracker_url)
    if _provider_url(context.tracker_type, options.get("url")) != expected:
        raise CiReviewDeniedError("Restricted CI provider host binding denied")
    return {**options, "url": expected}


def _provider_source(
    db: Session, context: CiAuthorizationContext, request: CiReviewRequest
) -> _ProviderRead:
    _review_flow(db, context)
    if context.tracker_type not in {"github", "gitlab"}:
        raise CiReviewDeniedError("Restricted CI provider is unsupported")
    tracker = crud.crud_tracker.get(
        db, id=context.tracker_id, account_id=str(context.account_id)
    )
    if tracker is None:
        raise CiReviewDeniedError("Restricted CI integration unavailable")
    try:
        source = _ProviderRead(
            context=context,
            request=request,
            tracker_key=tracker.resolved_api_key or "",
            tracker_options=_bound_provider_options(
                context,
                feedback_tracker_options(db, tracker),
            ),
        )
    except ValueError:
        raise CiReviewUnavailableError() from None
    crud.crud_ci_execution.release_read(db)
    return source


def _load_request_source(
    db: Session, context: CiAuthorizationContext, request: CiReviewRequest
) -> _ProviderRead:
    context = crud.crud_ci_principal.authorize(
        db, context=context, action=CiAction.TRIGGER
    )
    return _provider_source(db, context, request)


def _accepted_binding(source: _ProviderRead, pr: Any) -> CiReviewBinding:
    """Never accept remote control fields, clone URLs or alternate repositories."""
    context, request = source.context, source.request
    if not isinstance(pr, dict):
        raise CiReviewDeniedError("Restricted CI pull request verification denied")
    if context.tracker_type == "github":
        head, base = pr.get("head"), pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise CiReviewDeniedError("Restricted CI pull request verification denied")
        repositories = [side.get("repo") for side in (head, base)]
        number, sha, branch = pr.get("number"), head.get("sha"), base.get("ref")
        valid = pr.get("state") == "open" and all(
            isinstance(repo, dict)
            and str(repo.get("id")) == context.repository_identifier
            and repo.get("full_name") == context.repository_slug
            for repo in repositories
        )
    else:
        number, sha, branch = pr.get("iid"), pr.get("sha"), pr.get("target_branch")
        valid = pr.get("state") == "opened" and all(
            str(pr.get(name)) == context.repository_identifier
            for name in ("source_project_id", "target_project_id")
        )
    provider_id = pr.get("id")
    if (
        not valid
        or type(number) is not int
        or number != request.pr_number
        or sha != request.head_sha
        or type(provider_id) not in (str, int)
        or not str(provider_id)
        or not isinstance(branch, str)
    ):
        raise CiReviewDeniedError("Restricted CI pull request verification denied")
    values = asdict(context)
    values.pop("actions")
    values.pop("credential_version")
    try:
        return CiReviewBinding(
            **values,
            version=1,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            provider_pr_id=str(provider_id),
            base_branch=branch,
        )
    except ValueError:
        raise CiReviewDeniedError(
            "Restricted CI pull request verification denied"
        ) from None


class _ReviewGitHubTracker(GitHubTracker):
    """Avoid the general adapter's credential-bearing failure diagnostics."""

    async def _get_installation_token(self) -> str:
        if not self.github_installation_id:
            raise CiReviewUnavailableError()
        from preloop.plugins.proprietary.github_app.service import (
            get_github_app_service,
        )  # type: ignore[import-untyped]

        token = await get_github_app_service().get_installation_access_token(
            self.github_installation_id
        )
        if not isinstance(token, str) or not token:
            raise CiReviewUnavailableError()
        return token


async def _create_review_client(source: _ProviderRead) -> Any:
    """Build only the accepted provider, with no unbounded auth preflight."""
    context = source.context
    options = _bound_provider_options(context, source.tracker_options)
    if context.tracker_type == "github":
        auth_type = options.get("auth_type", "api_token")
        if auth_type == "api_token" and not source.tracker_key:
            raise CiReviewUnavailableError()
        return _ReviewGitHubTracker(
            str(context.tracker_id),
            source.tracker_key,
            options,
            auth_type=auth_type,
            github_installation_id=options.get("github_installation_id"),
        )
    client = GitLabTracker(
        str(context.tracker_id),
        source.tracker_key,
        options,
        initialize_client=False,
    )
    # Construction is local only; skip gl.auth() and bound the sole network read.
    client.gl = gitlab.Gitlab(client.url, private_token=source.tracker_key, timeout=20)
    return client


async def _read_provider(source: _ProviderRead) -> CiReviewBinding:
    """One bounded immutable-ID read using the trusted integration snapshot."""
    context = source.context
    try:
        async with asyncio.timeout(25):
            client = await _create_review_client(source)
            if client is None:
                raise CiReviewUnavailableError()
            repository = quote(context.repository_identifier, safe="")
            number = source.request.pr_number
            if context.tracker_type == "github":
                pr = await client._request(
                    "GET", f"/repositories/{repository}/pulls/{number}"
                )
            else:
                client.gl.timeout = 20
                # Read directly: adapter error logs can contain response bodies.
                pr = await asyncio.to_thread(
                    client.gl.http_get,
                    f"/projects/{repository}/merge_requests/{number}",
                )
            return _accepted_binding(source, pr)
    except TrackerResponseError as error:
        if error.status_code in {403, 404}:
            raise CiReviewDeniedError(
                "Restricted CI pull request verification denied"
            ) from None
        raise CiReviewUnavailableError() from None
    except (CiReviewDeniedError, CiReviewUnavailableError):
        raise
    except (TrackerError, gitlab.exceptions.GitlabError, httpx.HTTPError, TimeoutError):
        raise CiReviewUnavailableError() from None
    except Exception:  # Provider SDK errors may contain credentials or bodies.
        raise CiReviewUnavailableError() from None


def execution_projection(execution: models.FlowExecution) -> dict[str, Any]:
    """Expose correlation and readiness, never prompts, runtime keys or logs."""
    binding = CiReviewBinding.model_validate(execution.ci_review_binding)
    return {
        "id": str(execution.id),
        "flow_id": str(binding.flow_id),
        "project_id": str(binding.project_id),
        "repository_identifier": binding.repository_identifier,
        "pr_number": binding.pr_number,
        "provider_pr_id": binding.provider_pr_id,
        "head_sha": binding.head_sha,
        "status": execution.status,
        "start_time": execution.start_time,
        "end_time": execution.end_time,
        "failure_category": execution.failure_category,
    }


def _prepare_execution(
    db: Session, context: CiAuthorizationContext, binding: CiReviewBinding
) -> tuple[models.Flow, models.FlowExecution, dict[str, Any], dict[str, Any]]:
    crud.crud_ci_principal.authorize(db, context=context, action=CiAction.TRIGGER)
    flow = _review_flow(db, context)
    event = prepare_execution_routing(db, flow, ci_review_event(binding))
    event["test_mode"] = False
    attach_trigger_subject(event)
    execution = crud.crud_ci_execution.create(
        db, context=context, binding=binding, event=event
    )
    # Refresh after the creation commit; avoid expired ORM reads on the loop.
    refreshed_flow = crud.crud_flow.get(
        db, id=context.flow_id, account_id=context.account_id
    )
    if refreshed_flow is None:
        raise CiReviewDeniedError("Restricted CI flow unavailable")
    return refreshed_flow, execution, execution_projection(execution), dict(event)


async def trigger_ci_review(
    db: Session, *, context: CiAuthorizationContext, request: CiReviewRequest
) -> dict[str, Any]:
    """Validate before insert, then reuse the existing single-execution dispatch."""
    from preloop.services.flow_trigger_service import FlowTriggerService
    from preloop.sync.services.event_bus import get_nats_client

    try:
        source = await run_db_off_loop(
            lambda: _load_request_source(db, context, request)
        )
        binding = await _read_provider(source)
        flow, execution, projection, event = await run_db_off_loop(
            lambda: _prepare_execution(db, context, binding)
        )
    except SQLAlchemyError:
        raise CiReviewUnavailableError() from None
    try:
        # Current principal/binding, not the initiating key, governs accepted work.
        await run_db_off_loop(
            lambda: crud.crud_ci_execution.authorize_dispatch(db, execution=execution)
        )
        nats_client = await get_nats_client()
        await FlowTriggerService(db)._start_flow_execution(
            flow=flow,
            event_data=event,
            nats_client=nats_client,
            precreated_execution=execution,
        )
    except Exception:  # Existing dispatcher adapters have heterogeneous failures.
        await run_db_off_loop(
            lambda: crud.crud_ci_execution.reject_dispatch(db, execution=execution)
        )
        raise CiReviewUnavailableError(UUID(projection["id"])) from None
    return projection


def _load_dispatch_source(
    db: Session, execution: models.FlowExecution
) -> tuple[CiReviewBinding, _ProviderRead] | None:
    binding = crud.crud_ci_execution.authorize_dispatch(db, execution=execution)
    if binding is None:
        return None
    context = context_from_binding(binding)
    request = CiReviewRequest(pr_number=binding.pr_number, head_sha=binding.head_sha)
    return binding, _provider_source(db, context, request)


async def ensure_ci_dispatch_admission(
    db: Session, *, execution: models.FlowExecution
) -> None:
    """Reconcile accepted PR/head and current authority immediately before launch."""
    if (
        execution.ci_principal_id is None
        and execution.ci_review_binding is None
        and execution.initiating_ci_key_id is None
    ):
        return
    loaded = await run_db_off_loop(lambda: _load_dispatch_source(db, execution))
    if loaded is None:
        return
    expected, source = loaded
    current = await _read_provider(source)
    if current != expected:
        raise CiReviewDeniedError(
            "Restricted CI accepted review changed before dispatch"
        )
    await run_db_off_loop(
        lambda: crud.crud_ci_execution.authorize_dispatch(db, execution=execution)
    )


# Result artifacts are agent-authored; controller-private/runtime material is
# never part of the machine result contract, even when nested inside a report.
_PRIVATE_RESULT_FIELDS = frozenset(
    {
        "credentials",
        "credential",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "password",
        "secret",
        "secrets",
        "token",
        "env",
        "environment",
        "prompt",
        "prompt_template",
        "rendered_prompt",
        "mcp_logs",
        "mcp_config",
        "mcp_usage_logs",
        "resolved_prompt",
        "resolved_input_prompt",
        "auth_headers",
        "headers",
        "logs",
        "tool_calls",
        "mcp_servers",
        "environment_variables",
        "agent_config",
        "model_config",
        "runner_config",
        "git_clone_config",
        "workspace_seed",
        "workspace_files",
    }
)


def public_ci_result(value: Any) -> Any:
    """Project persisted report data without private controller fields."""
    if isinstance(value, dict):
        return {
            key: public_ci_result(item)
            for key, item in value.items()
            if isinstance(key, str)
            and not key.startswith("_")
            and key.lower().replace("-", "_") not in _PRIVATE_RESULT_FIELDS
            and not key.lower()
            .replace("-", "_")
            .endswith(
                (
                    "_api_key",
                    "_access_token",
                    "_refresh_token",
                    "_password",
                    "_secret",
                    "_credentials",
                )
            )
        }
    if isinstance(value, list):
        return [public_ci_result(item) for item in value]
    return value
