"""Operator-selected PR adoption with short, worker-owned database transactions."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from types import SimpleNamespace
from urllib.parse import quote, urlparse
from uuid import UUID

from preloop.config import settings
from preloop.agents.cli_session import valid_session_id
from preloop.models.crud import (
    crud_flow,
    crud_flow_execution,
    crud_flow_feedback,
    crud_tracker,
)
from preloop.models.crud import flow_artifact
from preloop.models.db.session import get_session_factory
from preloop.schemas.flow_continuation import (
    ContinuationAdoptRequest,
    ContinuationAdoptResponse,
    ContinuationPreview,
)
from preloop.services.flow_artifacts import artifact_thread_id, artifact_reference
from preloop.services.flow_feedback import feedback_policy, register_thread
from preloop.services.flow_feedback_provider import (
    FeedbackProvider,
    feedback_tracker_options,
)
from preloop.sync.trackers.factory import create_tracker_client


class ContinuationAdoptionError(ValueError):
    """A safe, operator-readable adoption precondition failure."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _load_source(account_id: UUID, execution_id: UUID) -> dict[str, Any]:
    """Materialize only the selected source, then release DB before provider I/O."""
    with get_session_factory()() as db:
        execution = crud_flow_execution.get(db, id=execution_id, account_id=account_id)
        if execution is None:
            raise ContinuationAdoptionError("Flow execution not found", 404)
        flow = crud_flow.get(db, id=execution.flow_id)
        if flow is None or flow.account_id != account_id:
            raise ContinuationAdoptionError("Flow execution not found", 404)
        if execution.status != "SUCCEEDED":
            raise ContinuationAdoptionError(
                "Only a successful publishing execution can be adopted"
            )
        details = execution.trigger_event_details or {}
        if details.get("_thread_id"):
            raise ContinuationAdoptionError("Select the original publishing execution")
        result = execution.result or {}
        pr_url, branch = result.get("pr_url"), result.get("pr_source_branch")
        if not isinstance(pr_url, str) or not isinstance(branch, str) or not branch:
            raise ContinuationAdoptionError(
                "Execution has no recorded PR and source branch"
            )
        payload = details.get("payload") or {}
        repository = payload.get("repository") or payload.get("project") or {}
        repository_id = repository.get("id")
        provider = details.get("source")
        number = urlparse(pr_url).path.rstrip("/").split("/")[-1]
        try:
            tracker_id = UUID(
                str(details.get("tracker_id") or flow.trigger_event_source)
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContinuationAdoptionError(
                "Execution has no valid tracker binding"
            ) from exc
        if (
            provider not in {"github", "gitlab"}
            or not repository_id
            or not number.isdigit()
        ):
            raise ContinuationAdoptionError(
                "Execution has no valid provider PR binding"
            )
        tracker = crud_tracker.get(db, id=tracker_id)
        if (
            tracker is None
            or tracker.account_id != account_id
            or tracker.tracker_type != provider
        ):
            raise ContinuationAdoptionError("Execution tracker is unavailable", 404)
        thread_id = artifact_thread_id(details, execution_id)
        workspace = flow_artifact.latest(
            db,
            account_id=account_id,
            flow_id=flow.id,
            thread_id=thread_id,
            execution_id=execution_id,
            kind="workspace",
        )
        cli = execution.cli_session or {}
        reference = cli.get("artifact_reference") or {}
        native = None
        if reference.get("artifact_id"):
            try:
                native = flow_artifact.get(
                    db,
                    artifact_id=UUID(str(reference["artifact_id"])),
                    account_id=account_id,
                    flow_id=flow.id,
                    thread_id=thread_id,
                )
            except (ValueError, TypeError):
                native = None
        now = datetime.now(UTC)

        def available(artifact: Any) -> bool:
            return bool(
                artifact is not None
                and artifact.execution_id == execution_id
                and artifact.ciphertext is not None
                and artifact.expires_at.replace(tzinfo=UTC) > now
            )

        bindings = crud_flow_feedback.find(
            db,
            account_id=account_id,
            tracker_id=tracker_id,
            repository_id=str(repository_id),
            pr_number=number,
        )
        existing = next((row for row in bindings if row.flow_id == flow.id), None)
        return {
            "execution_id": execution_id,
            "flow_id": flow.id,
            "pr_url": pr_url,
            "branch": branch,
            "provider": provider,
            "repository_id": str(repository_id),
            "number": number,
            "feedback_enabled": bool(flow.is_enabled and feedback_policy(flow)),
            "policy": deepcopy(feedback_policy(flow) or {}),
            "native_resume_available": bool(
                valid_session_id(cli.get("agent_type", ""), cli.get("session_id", ""))
                and available(workspace)
                and available(native)
                and native.kind == "native_session"
                and str(reference.get("execution_id")) == str(execution_id)
                and reference.get("manifest_sha256")
                == artifact_reference(native).manifest_sha256
            ),
            "existing_thread_id": existing.id if existing else None,
            "existing_thread_state": existing.state if existing else None,
            # Never project tracker credentials into the API response.
            "tracker_id": tracker_id,
            "tracker_key": tracker.resolved_api_key,
            "tracker_options": feedback_tracker_options(db, tracker),
        }


class _BoundedReadClient:
    """A single-publication preflight has at most twelve provider requests."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.remaining = 12
        self.gl = getattr(client, "gl", None)

    def _take(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ContinuationAdoptionError("Provider preflight request limit reached")

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        self._take()
        if method != "GET" and not (method == "POST" and path == "/graphql"):
            raise ContinuationAdoptionError("Provider preflight permits reads only")
        return await self.client._request(method, path, *args, **kwargs)

    async def _make_request(self, method: Any, path: str) -> Any:
        self._take()
        return await self.client._make_request(method, path)


async def _feedback_preflight(
    client: _BoundedReadClient, source: dict[str, Any], publication: dict[str, Any]
) -> dict[str, Any]:
    """Exercise the same review/check/protection reads used by reconciliation."""
    thread = SimpleNamespace(
        provider=source["provider"],
        repository_id=source["repository_id"],
        pr_number=source["number"],
        policy=source["policy"],
    )
    try:
        state = await FeedbackProvider(client, thread).read()
        publication["open"] = publication["open"] and not state.closed
        publication["feedback_readable"] = state.blocked_reason != "provider_page_limit"
        publication["feedback_blocked_reason"] = state.blocked_reason
        if state.head_sha != publication["head_sha"]:
            raise ContinuationAdoptionError("PR head changed during preflight")
    except ContinuationAdoptionError:
        raise
    except Exception:
        publication["feedback_readable"] = False
        publication["feedback_blocked_reason"] = "provider_reconciliation_unavailable"
    return publication


async def _bounded_publication(source: dict[str, Any]) -> dict[str, Any]:
    async with asyncio.timeout(25):
        return await _read_publication(source)


async def _read_publication(source: dict[str, Any]) -> dict[str, Any]:
    """Read exactly one current PR without holding a database session."""
    client = await create_tracker_client(
        source["provider"],
        str(source["tracker_id"]),
        source["tracker_key"],
        source["tracker_options"],
    )
    if client is None:
        raise ContinuationAdoptionError("Tracker cannot read the published PR")
    client = _BoundedReadClient(client)
    repository = quote(source["repository_id"], safe="")
    number = quote(source["number"], safe="")
    if source["provider"] == "github":
        pr = await client._request("GET", f"/repositories/{repository}/pulls/{number}")
        same_repository = all(
            str((pr.get(side, {}).get("repo") or {}).get("id"))
            == source["repository_id"]
            for side in ("base", "head")
        )
        publication = {
            "open": pr.get("state") == "open",
            "same_repository": same_repository,
            "branch": pr.get("head", {}).get("ref"),
            "head_sha": pr.get("head", {}).get("sha"),
            "pr_url": pr.get("html_url"),
        }
        return await _feedback_preflight(client, source, publication)
    path = f"/projects/{repository}/merge_requests/{number}"
    pr = await client._make_request(client.gl.http_get, path)
    publication = {
        "open": pr.get("state") == "opened",
        "same_repository": all(
            str(pr.get(key)) == source["repository_id"]
            for key in ("source_project_id", "target_project_id")
        ),
        "branch": pr.get("source_branch"),
        "head_sha": pr.get("sha"),
        "pr_url": pr.get("web_url"),
    }

    return await _feedback_preflight(client, source, publication)


def preview_continuation(account_id: UUID, execution_id: UUID) -> ContinuationPreview:
    """Preview one source in a FastAPI worker; no activation or provider writes."""
    try:
        source = _load_source(account_id, execution_id)
    except ContinuationAdoptionError:
        raise
    except ValueError as exc:
        raise ContinuationAdoptionError(
            "Execution tracker configuration is unavailable"
        ) from exc
    try:
        publication = asyncio.run(_bounded_publication(source))
    except ContinuationAdoptionError:
        raise
    except Exception as exc:
        raise ContinuationAdoptionError(
            "Unable to verify the current published PR"
        ) from exc
    if (
        not publication["open"]
        or not publication["same_repository"]
        or publication["branch"] != source["branch"]
        or str(publication["pr_url"]).rstrip("/") != source["pr_url"].rstrip("/")
        or not publication["head_sha"]
    ):
        raise ContinuationAdoptionError(
            "Published PR is closed or its repository/branch binding changed"
        )
    warnings = []
    if not publication.get("feedback_readable"):
        warnings.append(
            "Tracker cannot verify the PR review, CI and repository gates. Check its read permissions."
        )
    elif (
        publication.get("feedback_blocked_reason")
        == "repository_requirements_unavailable"
    ):
        warnings.append(
            "Repository requirements are unavailable. Known review and CI feedback can be repaired, but readiness cannot be verified."
        )
    elif publication.get("feedback_blocked_reason"):
        warnings.append(
            "Feedback reconciliation is blocked: "
            + publication["feedback_blocked_reason"]
        )
    if not source["feedback_enabled"]:
        warnings.append("Enable PR follow-up on this flow before adopting the PR.")
    if not settings.flow_artifact_direct_upload:
        warnings.append("Direct checkpoint uploads must be enabled before adoption.")
    if not source["native_resume_available"]:
        warnings.append(
            "Recovery files are unavailable. Continuing requires a fresh conversation on the published PR branch."
        )
    return ContinuationPreview(
        **{
            key: source[key]
            for key in (
                "execution_id",
                "flow_id",
                "pr_url",
                "branch",
                "feedback_enabled",
                "native_resume_available",
                "existing_thread_id",
                "existing_thread_state",
            )
        },
        head_sha=publication["head_sha"],
        feedback_readable=publication.get("feedback_readable", False),
        feedback_blocked_reason=publication.get("feedback_blocked_reason"),
        artifact_upload_enabled=settings.flow_artifact_direct_upload,
        allowed_recovery_modes=["native_resume"]
        if source["native_resume_available"]
        else ["published_branch_handoff"],
        warnings=warnings,
    )


def adopt_continuation(
    account_id: UUID, execution_id: UUID, request: ContinuationAdoptRequest
) -> ContinuationAdoptResponse:
    """Revalidate the selected PR and atomically register one bounded thread."""
    preview = preview_continuation(account_id, execution_id)
    if not preview.feedback_readable:
        raise ContinuationAdoptionError(
            "Tracker cannot read the required feedback and repository gates"
        )
    if preview.head_sha != request.expected_head_sha:
        raise ContinuationAdoptionError("PR head changed; preview the PR again")
    if not preview.feedback_enabled or not preview.artifact_upload_enabled:
        raise ContinuationAdoptionError(
            "Flow feedback and direct checkpoint uploads must be enabled"
        )
    if request.recovery_mode not in preview.allowed_recovery_modes:
        raise ContinuationAdoptionError("Requested recovery mode is unavailable")
    if (
        request.recovery_mode == "published_branch_handoff"
        and not request.acknowledge_fresh_conversation
    ):
        raise ContinuationAdoptionError(
            "Acknowledge starting a fresh conversation from the published branch"
        )
    with get_session_factory()() as db:
        execution = crud_flow_execution.get(db, id=execution_id, account_id=account_id)
        if execution is None or execution.status != "SUCCEEDED":
            raise ContinuationAdoptionError(
                "Publishing execution is no longer available"
            )
        result = execution.result or {}
        if (
            result.get("pr_url") != preview.pr_url
            or result.get("pr_source_branch") != preview.branch
        ):
            raise ContinuationAdoptionError("Publishing execution binding changed")
        flow = crud_flow.get(db, id=execution.flow_id)
        if flow is None or not flow.is_enabled or not feedback_policy(flow):
            raise ContinuationAdoptionError("Flow feedback is no longer enabled")
        thread = register_thread(
            db,
            execution,
            preview.pr_url,
            preview.branch,
            adoption={
                "source_execution_id": str(execution_id),
                "recovery_mode": request.recovery_mode,
                "acknowledged_head_sha": preview.head_sha,
                "acknowledged_at": datetime.now(UTC).isoformat(),
            },
        )
        if thread is None:
            raise ContinuationAdoptionError(
                "Execution cannot be registered for continuation"
            )
        recorded = (thread.context or {}).get("adoption") or {}
        return ContinuationAdoptResponse(
            thread_id=thread.id,
            state=thread.state,
            pr_url=thread.pr_url,
            recovery_mode=recorded.get("recovery_mode", "native_resume"),
        )
