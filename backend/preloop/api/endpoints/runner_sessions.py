"""Remote harness sessions on personal runners: HTTP API (#1483, contract C).

The console (and later the mobile apps) start a session with a harness that
is installed on one of the caller's runners, send it turns and stop it. The
routes here own authorization, validation, the clone credential hand-off and
the audit trail. Persisting the session and talking to the runner belong to
the runner session service (#1482), which registers itself through
:func:`register_runner_session_service`. Until it does, every route except
``session-options`` answers 503 ``remote_sessions_unavailable`` after it has
authorized, validated and audited the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Union,
)
from uuid import UUID, uuid4

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_audit_log,
    crud_project,
    crud_tracker,
)
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.models.schemas.flow_runner import HarnessId, HarnessInventory
from preloop.models.schemas.runner_session import RunnerSessionStopMode
from preloop.plugins.account_hooks import (
    AuthorizationContext,
    may_start_runner_session,
)
from preloop.utils.permissions import require_permission, user_holds_permission

router = APIRouter()
logger = logging.getLogger(__name__)

FlowRunner = models.FlowRunner
User = models.User

#: Wave 1 defaults; the runner enforces its own (possibly lower) limits too.
DEFAULT_MAX_CONCURRENT_SESSIONS = 2
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
MAX_FIRST_PROMPT_CHARS = 32_000
MAX_TURN_CHARS = 32_000
MAX_REPOSITORIES_PER_TRACKER = 200

#: Tracker types that can back a ``tracker_checkout`` workspace in wave 1.
CHECKOUT_PROVIDERS: Dict[str, str] = {
    "github": "github",
    "bitbucket": "bitbucket_cloud",
}
#: Session states in which the session still counts against the limit.
ACTIVE_STATES = frozenset({"requested", "starting", "idle", "running", "stopping"})
ONLINE_RUNNER_STATUSES = frozenset({"online", "busy"})
AUDIT_RESOURCE_TYPE = "runner_session"


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class AuthorizedDirectoryWorkspace(BaseModel):
    """Contract D: a directory the host owner authorized for remote sessions."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["authorized_directory"]
    id: str = Field(min_length=1, max_length=64)


class TrackerCheckoutWorkspace(BaseModel):
    """Contract D: a fresh checkout of a tracker repository on the host."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["tracker_checkout"]
    tracker_id: UUID
    repository: str = Field(min_length=1, max_length=255)
    ref: Optional[str] = Field(None, max_length=255)


SessionWorkspace = Annotated[
    Union[AuthorizedDirectoryWorkspace, TrackerCheckoutWorkspace],
    Field(discriminator="kind"),
]


class RunnerSessionStartRequest(BaseModel):
    harness: HarnessId
    model: Optional[str] = Field(None, max_length=128)
    workspace: SessionWorkspace
    first_prompt: Optional[str] = Field(None, max_length=MAX_FIRST_PROMPT_CHARS)
    title: Optional[str] = Field(None, max_length=255)


class RunnerSessionStartResponse(BaseModel):
    session_id: str
    remote_session_id: str
    state: str = "requested"


class RunnerSessionTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TURN_CHARS)


class RunnerSessionTurnResponse(BaseModel):
    turn_id: str
    state: str = "queued"


class RunnerSessionStopRequest(BaseModel):
    mode: RunnerSessionStopMode = "graceful"


class RunnerSessionStopResponse(BaseModel):
    session_id: str
    state: str


class SessionOptionModel(BaseModel):
    id: str
    source: str


class SessionOptionHarness(BaseModel):
    harness: str
    display_name: str
    session_mode: str
    models: List[SessionOptionModel] = Field(default_factory=list)
    #: Additive: false when the harness is installed but cannot start a
    #: session right now; ``unavailable_reason`` says why.
    available: bool = True
    unavailable_reason: Optional[str] = None


class SessionOptionDirectory(BaseModel):
    id: str
    label: str
    mode: str
    harnesses: Union[List[str], Literal["all"]] = "all"


class SessionOptionRepository(BaseModel):
    full_name: str
    default_branch: Optional[str] = None


class SessionOptionCheckoutSource(BaseModel):
    tracker_id: str
    tracker_name: Optional[str] = None
    provider: str
    repositories: List[SessionOptionRepository] = Field(default_factory=list)


class SessionOptionLimits(BaseModel):
    max_concurrent: int
    active: int
    idle_timeout_seconds: int


class RunnerSessionOptions(BaseModel):
    runner_id: str
    online: bool
    #: False until the runner session service (#1482) is installed.
    sessions_available: bool = True
    harnesses: List[SessionOptionHarness] = Field(default_factory=list)
    authorized_directories: List[SessionOptionDirectory] = Field(default_factory=list)
    checkout_sources: List[SessionOptionCheckoutSource] = Field(default_factory=list)
    limits: SessionOptionLimits


class RunnerSessionListItem(BaseModel):
    session_id: str
    remote_session_id: Optional[str] = None
    state: str
    harness: str
    model: Optional[str] = None
    actor_user_id: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    end_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Runner session service seam (#1482 implements it)
# ---------------------------------------------------------------------------


@dataclass
class RemoteSessionRecord:
    """What the endpoint needs to know about one stored remote session."""

    session_id: str
    remote_session_id: str
    runner_id: str
    account_id: str
    actor_user_id: Optional[str]
    harness: str
    model: Optional[str]
    state: str
    workspace: Dict[str, Any] = field(default_factory=dict)
    turn_in_progress: bool = False
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    end_reason: Optional[str] = None


@dataclass
class SessionStartPlan:
    """A validated start request, handed to the service."""

    runner: Any
    actor: Any
    remote_session_id: str
    harness: str
    model: Optional[str]
    #: Workspace spec as stored (never contains a credential).
    workspace: Dict[str, Any]
    #: Short-lived clone credential, only for the ``session_start`` frame.
    credential: Optional[Dict[str, Any]]
    first_prompt: Optional[str]
    title: Optional[str]
    limits: Dict[str, Any]


class RunnerSessionError(Exception):
    """Raised by the service when the runner cannot take the request."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class RunnerSessionService(Protocol):
    """Persistence and runner delivery for remote sessions (#1482)."""

    def count_active(self, db: Session, *, runner_id: str) -> int: ...

    def list_for_runner(
        self, db: Session, *, runner_id: str, limit: int
    ) -> List[RemoteSessionRecord]: ...

    def get(self, db: Session, *, session_id: str) -> Optional[RemoteSessionRecord]:
        """Look up by runtime session id or remote session id."""
        ...

    async def start(self, db: Session, plan: SessionStartPlan) -> RemoteSessionRecord:
        """Persist the session and send ``session_start``.

        The endpoint's ``count_active`` check is an early, best-effort
        refusal. ``start`` must reserve the slot atomically (row lock or
        conditional insert) and raise ``max_concurrent_reached`` when two
        requests race; the runner enforces its own limit as the final
        backstop. The routes are sync and run on the threadpool; the async
        service methods are awaited on the event loop (``from_thread.run``)
        because the runner socket lives there, so they must keep their own
        blocking database work off the loop.

        Raises:
            RunnerSessionError: ``runner_offline`` when the runner socket is
                not reachable, or any other code the endpoint returns as 409.
        """
        ...

    async def send_turn(
        self, db: Session, record: RemoteSessionRecord, *, turn_id: str, text: str
    ) -> None: ...

    async def stop(
        self, db: Session, record: RemoteSessionRecord, *, mode: str
    ) -> RemoteSessionRecord: ...


_service: Optional[RunnerSessionService] = None


def register_runner_session_service(service: Optional[RunnerSessionService]) -> None:
    """Install (or clear, with ``None``) the runner session service."""
    global _service
    _service = service


def get_runner_session_service() -> Optional[RunnerSessionService]:
    """Return the installed runner session service, or ``None``."""
    return _service


# ---------------------------------------------------------------------------
# Clone credential (#1484). Stub until runner_workspace_credentials lands.
# ---------------------------------------------------------------------------


class CheckoutCredentialError(Exception):
    """A clone credential could not be minted; ``code`` is the API code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _mint_checkout_credential_stub(
    db: Session, *, tracker: Any, repository: str
) -> Dict[str, Any]:
    raise CheckoutCredentialError(
        "checkout_not_available",
        "Repository checkouts for remote sessions are not available yet.",
    )


try:  # pragma: no cover - depends on whether #1484 has landed
    from preloop.services.runner_workspace_credentials import (  # type: ignore[import-not-found]
        mint_checkout_credential as _mint_checkout_credential,
    )
except ImportError:  # pragma: no cover
    _mint_checkout_credential = _mint_checkout_credential_stub


def mint_checkout_credential(
    db: Session, *, tracker: Any, repository: str
) -> Dict[str, Any]:
    """Mint a short-lived clone credential, mapping failures to API codes."""
    try:
        return _mint_checkout_credential(db, tracker=tracker, repository=repository)
    except CheckoutCredentialError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            raise CheckoutCredentialError(code, str(exc)) from exc
        logger.warning("checkout credential minting failed: %s", type(exc).__name__)
        raise CheckoutCredentialError("checkout_failed") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _is_account_admin(db: Session, user: Any) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    account = crud_account.get(db, id=user.account_id)
    if account is not None and str(account.primary_user_id) == str(user.id):
        return True
    return user_holds_permission(db, user, "manage_account")


def _get_runner(db: Session, runner_id: UUID, user: Any) -> Any:
    runner = crud_flow_runner.get(db, id=runner_id, account_id=str(user.account_id))
    if runner is None:
        raise _error(404, "runner_not_found", "Runner not found")
    return runner


def _runner_online(runner: Any) -> bool:
    return (runner.status or "").lower() in ONLINE_RUNNER_STATUSES


def _runner_field(runner: Any, name: str) -> Any:
    """Read a contract A field from its column, or from capabilities.

    The ``harness_inventory`` column lands with #1480; until then a runner
    that already reports it has it under ``capabilities``.
    """
    value = getattr(runner, name, None)
    if value is None:
        capabilities = getattr(runner, "capabilities", None) or {}
        if isinstance(capabilities, Mapping):
            value = capabilities.get(name)
    return value


def _inventory_entries(runner: Any) -> List[Any]:
    raw = _runner_field(runner, "harness_inventory")
    if not raw:
        return []
    try:
        return HarnessInventory.model_validate(raw).entries
    except Exception:
        logger.info("runner %s: unreadable harness_inventory ignored", runner.id)
        return []


def _harness_unavailable_reason(entry: Any) -> Optional[str]:
    if not entry.enabled:
        return "harness_disabled"
    if entry.login_state == "signed_out":
        return "harness_signed_out"
    if not entry.sessions_enabled or entry.session_mode == "none":
        return "harness_not_enabled_for_sessions"
    return None


def _session_harnesses(runner: Any) -> List[SessionOptionHarness]:
    result = []
    for entry in _inventory_entries(runner):
        if entry.support_level != "flows_and_sessions":
            continue
        reason = _harness_unavailable_reason(entry)
        result.append(
            SessionOptionHarness(
                harness=entry.harness,
                display_name=entry.display_name,
                session_mode=entry.session_mode,
                models=[
                    SessionOptionModel(id=model.id, source=model.source)
                    for model in entry.models
                ],
                available=reason is None,
                unavailable_reason=reason,
            )
        )
    return result


def _authorized_directories(runner: Any) -> List[SessionOptionDirectory]:
    raw = _runner_field(runner, "authorized_directories") or []
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        harnesses = item.get("harnesses", "all")
        if harnesses != "all" and not isinstance(harnesses, list):
            harnesses = "all"
        try:
            result.append(
                SessionOptionDirectory(
                    id=str(item["id"]),
                    label=str(item.get("label") or item["id"]),
                    mode=str(item.get("mode") or "read_only"),
                    harnesses=harnesses,
                )
            )
        except Exception:
            continue
    return result


def _checkout_sources(db: Session, user: Any) -> List[SessionOptionCheckoutSource]:
    sources = []
    for tracker in crud_tracker.get_for_account(db, account_id=str(user.account_id)):
        provider = CHECKOUT_PROVIDERS.get(str(tracker.tracker_type or "").lower())
        if provider is None or not tracker.is_active:
            continue
        repositories = []
        for project in crud_project.get_for_tracker(
            db,
            tracker_id=str(tracker.id),
            account_id=str(user.account_id),
            limit=MAX_REPOSITORIES_PER_TRACKER,
        ):
            if not project.is_active:
                continue
            full_name = project.slug or project.identifier
            if not full_name or "/" not in full_name:
                continue
            meta = project.meta_data if isinstance(project.meta_data, dict) else {}
            branch = meta.get("default_branch")
            repositories.append(
                SessionOptionRepository(
                    full_name=full_name,
                    default_branch=branch if isinstance(branch, str) else None,
                )
            )
        sources.append(
            SessionOptionCheckoutSource(
                tracker_id=str(tracker.id),
                tracker_name=tracker.name,
                provider=provider,
                repositories=sorted(repositories, key=lambda r: r.full_name),
            )
        )
    return sources


def _limits(db: Session, runner: Any) -> Dict[str, int]:
    service = _service
    active = service.count_active(db, runner_id=str(runner.id)) if service else 0
    return {
        "max_concurrent": DEFAULT_MAX_CONCURRENT_SESSIONS,
        "active": active,
        "idle_timeout_seconds": DEFAULT_IDLE_TIMEOUT_SECONDS,
    }


@dataclass
class _AuditContext:
    db: Session
    user: Any
    runner: Any
    harness: Optional[str] = None
    model: Optional[str] = None
    workspace_kind: Optional[str] = None
    workspace_label: Optional[str] = None

    def details(self, **extra: Any) -> Dict[str, Any]:
        details = {
            "actor_user_id": str(self.user.id),
            "runner_id": str(self.runner.id),
            "runner_name": self.runner.name,
            "host": self.runner.hostname,
            "harness": self.harness,
            "model": self.model,
            "workspace_kind": self.workspace_kind,
            "workspace_label": self.workspace_label,
        }
        details.update(extra)
        return details

    def log(
        self,
        action: str,
        *,
        status_value: str = "success",
        resource_id: Optional[str] = None,
        **extra: Any,
    ) -> None:
        try:
            crud_audit_log.log_action(
                self.db,
                account_id=self.user.account_id,
                user_id=self.user.id,
                action=action,
                resource_type=AUDIT_RESOURCE_TYPE,
                resource_id=resource_id or str(self.runner.id),
                status=status_value,
                details=self.details(**extra),
            )
        except Exception:
            # An audit failure must not leave the request half done, but it
            # must be visible.
            logger.exception("runner session audit write failed: %s", action)
            self.db.rollback()

    def reject(self, status_code: int, code: str, message: str) -> HTTPException:
        self.log(
            "runner_session.rejected",
            status_value="denied" if status_code == 403 else "failure",
            reason=code,
        )
        return _error(status_code, code, message)


def _authorize(audit: _AuditContext) -> None:
    decision = may_start_runner_session(
        AuthorizationContext(
            account_id=audit.user.account_id, db=audit.db, user=audit.user
        ),
        audit.runner,
        is_account_admin=_is_account_admin(audit.db, audit.user),
    )
    if not decision.allowed:
        raise audit.reject(
            403,
            decision.reason or "not_runner_owner",
            "Only the runner owner or an account admin can use sessions on "
            "this runner.",
        )


def _require_service(audit: _AuditContext) -> RunnerSessionService:
    service = _service
    if service is None:
        raise audit.reject(
            503,
            "remote_sessions_unavailable",
            "Remote sessions are not available on this server yet.",
        )
    return service


def _record_to_item(record: RemoteSessionRecord) -> RunnerSessionListItem:
    return RunnerSessionListItem(
        session_id=record.session_id,
        remote_session_id=record.remote_session_id,
        state=record.state,
        harness=record.harness,
        model=record.model,
        actor_user_id=record.actor_user_id,
        started_at=record.started_at,
        last_activity_at=record.last_activity_at,
        end_reason=record.end_reason,
    )


def _session_audit(
    db: Session, user: Any, session_id: str
) -> tuple[RunnerSessionService, RemoteSessionRecord, _AuditContext]:
    service = _service
    if service is None:
        raise _error(
            503,
            "remote_sessions_unavailable",
            "Remote sessions are not available on this server yet.",
        )
    record = service.get(db, session_id=session_id)
    if record is None or str(record.account_id) != str(user.account_id):
        raise _error(404, "session_not_found", "Session not found")
    runner = crud_flow_runner.get(
        db, id=UUID(str(record.runner_id)), account_id=str(user.account_id)
    )
    if runner is None:
        raise _error(404, "session_not_found", "Session not found")
    workspace = record.workspace or {}
    audit = _AuditContext(
        db=db,
        user=user,
        runner=runner,
        harness=record.harness,
        model=record.model,
        workspace_kind=workspace.get("kind"),
        workspace_label=workspace.get("label"),
    )
    _authorize(audit)
    return service, record, audit


def _prepare_turn(
    db: Session, user: Any, session_id: str
) -> tuple[RunnerSessionService, RemoteSessionRecord, _AuditContext]:
    """Authorize a turn and refuse it while the session cannot take one."""
    service, record, audit = _session_audit(db, user, session_id)
    if record.state not in ACTIVE_STATES or record.state == "stopping":
        raise audit.reject(409, "session_ended", "The session has ended.")
    if record.turn_in_progress or record.state == "running":
        raise audit.reject(
            409, "turn_in_progress", "Wait for the current turn to finish."
        )
    return service, record, audit


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/runners/{runner_id}/session-options", response_model=RunnerSessionOptions)
@require_permission("view_flows")
def get_runner_session_options(
    runner_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RunnerSessionOptions:
    """What the caller may start on this runner: harnesses, workspaces, limits."""
    runner = _get_runner(db, runner_id, current_user)
    # A denied read is audited like a denied start: probing someone else's
    # runner must leave a trace.
    _authorize(_AuditContext(db=db, user=current_user, runner=runner))
    return RunnerSessionOptions(
        runner_id=str(runner.id),
        online=_runner_online(runner),
        sessions_available=_service is not None,
        harnesses=_session_harnesses(runner),
        authorized_directories=_authorized_directories(runner),
        checkout_sources=_checkout_sources(db, current_user),
        limits=SessionOptionLimits(**_limits(db, runner)),
    )


def _prepare_start(
    db: Session, current_user: Any, runner_id: UUID, body: RunnerSessionStartRequest
) -> tuple[RunnerSessionService, SessionStartPlan, _AuditContext]:
    """Authorize, validate, mint and audit a start request (blocking, off-loop).

    Raises:
        HTTPException: A refusal, already written to the audit log.
    """
    runner = _get_runner(db, runner_id, current_user)
    workspace = body.workspace
    audit = _AuditContext(
        db=db,
        user=current_user,
        runner=runner,
        harness=body.harness,
        model=body.model,
        workspace_kind=workspace.kind,
    )
    if isinstance(workspace, AuthorizedDirectoryWorkspace):
        directory = next(
            (d for d in _authorized_directories(runner) if d.id == workspace.id),
            None,
        )
        audit.workspace_label = directory.label if directory else workspace.id
    else:
        audit.workspace_label = workspace.repository

    _authorize(audit)
    if not _runner_online(runner):
        raise audit.reject(409, "runner_offline", "The runner is offline.")

    entry = next(
        (e for e in _session_harnesses(runner) if e.harness == body.harness), None
    )
    if entry is None or not entry.available:
        code = (
            entry.unavailable_reason
            if entry and entry.unavailable_reason
            else "harness_not_enabled_for_sessions"
        )
        raise audit.reject(
            409,
            code,
            "This harness is not enabled for remote sessions on the runner. "
            "On the host run: preloop runner sessions enable " + body.harness,
        )

    if isinstance(workspace, AuthorizedDirectoryWorkspace):
        if directory is None or (
            directory.harnesses != "all" and body.harness not in directory.harnesses
        ):
            raise audit.reject(
                409,
                "workspace_not_authorized",
                "The directory is not authorized for this harness on the runner.",
            )
        stored_workspace: Dict[str, Any] = {
            "kind": workspace.kind,
            "id": workspace.id,
            "label": directory.label,
            "mode": directory.mode,
        }
    else:
        tracker = crud_tracker.get_by_id_and_account(
            db, id=str(workspace.tracker_id), account_id=str(current_user.account_id)
        )
        provider = (
            CHECKOUT_PROVIDERS.get(str(tracker.tracker_type or "").lower())
            if tracker is not None
            else None
        )
        if tracker is None or provider is None:
            raise audit.reject(
                409,
                "checkout_source_not_supported",
                "Checkouts are supported for GitHub and Bitbucket Cloud trackers.",
            )
        stored_workspace = {
            "kind": workspace.kind,
            "tracker_id": str(workspace.tracker_id),
            "provider": provider,
            "repository": workspace.repository,
            "ref": workspace.ref,
            "label": workspace.repository,
        }

    service = _require_service(audit)
    limits = _limits(db, runner)
    if limits["active"] >= limits["max_concurrent"]:
        raise audit.reject(
            409,
            "max_concurrent_reached",
            "This runner already has the maximum number of remote sessions.",
        )

    credential: Optional[Dict[str, Any]] = None
    if isinstance(workspace, TrackerCheckoutWorkspace):
        try:
            credential = mint_checkout_credential(
                db, tracker=tracker, repository=workspace.repository
            )
        except CheckoutCredentialError as exc:
            raise audit.reject(
                409, exc.code, "A checkout credential could not be issued."
            ) from None
        audit.log(
            "runner_session.checkout_credential_minted",
            provider=stored_workspace["provider"],
            repository=workspace.repository,
            expires_at=str(credential.get("expires_at")) if credential else None,
        )

    remote_session_id = str(uuid4())
    audit.log(
        "runner_session.start_requested",
        resource_id=remote_session_id,
        remote_session_id=remote_session_id,
    )
    plan = SessionStartPlan(
        runner=runner,
        actor=current_user,
        remote_session_id=remote_session_id,
        harness=body.harness,
        model=body.model,
        workspace=stored_workspace,
        credential=credential,
        first_prompt=body.first_prompt,
        title=body.title,
        limits=limits,
    )
    return service, plan, audit


@router.post(
    "/runners/{runner_id}/sessions",
    response_model=RunnerSessionStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("execute_flows")
def start_runner_session(
    runner_id: UUID,
    body: RunnerSessionStartRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RunnerSessionStartResponse:
    """Start a harness session on a runner. Every outcome is audited.

    Sync on purpose: FastAPI runs it on the threadpool, so the database work
    never blocks the event loop. Only the service call, which talks to the
    runner socket owned by the loop, is handed back to it.
    """
    service, plan, audit = _prepare_start(db, current_user, runner_id, body)
    try:
        record = from_thread.run(service.start, db, plan)
    except RunnerSessionError as exc:
        raise audit.reject(409, exc.code, exc.message) from None
    finally:
        # The credential only ever travels in the session_start frame.
        plan.credential = None
    return RunnerSessionStartResponse(
        session_id=record.session_id,
        remote_session_id=record.remote_session_id,
        state=record.state,
    )


@router.get("/runners/{runner_id}/sessions", response_model=List[RunnerSessionListItem])
@require_permission("view_flows")
def list_runner_sessions(
    runner_id: UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> List[RunnerSessionListItem]:
    """Recent remote sessions on one runner (owner and admins)."""
    runner = _get_runner(db, runner_id, current_user)
    _authorize(_AuditContext(db=db, user=current_user, runner=runner))
    service = _service
    if service is None:
        return []
    records = service.list_for_runner(
        db, runner_id=str(runner.id), limit=max(1, min(limit, 200))
    )
    return [_record_to_item(record) for record in records]


@router.post(
    "/runner-sessions/{session_id}/turns",
    response_model=RunnerSessionTurnResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("execute_flows")
def send_runner_session_turn(
    session_id: str,
    body: RunnerSessionTurnRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RunnerSessionTurnResponse:
    """Send the next prompt. Wave 1 allows one running turn at a time."""
    service, record, audit = _prepare_turn(db, current_user, session_id)
    turn_id = str(uuid4())
    try:
        from_thread.run(
            lambda: service.send_turn(db, record, turn_id=turn_id, text=body.text)
        )
    except RunnerSessionError as exc:
        raise audit.reject(409, exc.code, exc.message) from None
    audit.log(
        "runner_session.turn_sent",
        resource_id=record.remote_session_id,
        remote_session_id=record.remote_session_id,
        turn_id=turn_id,
        text_length=len(body.text),
    )
    return RunnerSessionTurnResponse(turn_id=turn_id, state="queued")


@router.post(
    "/runner-sessions/{session_id}/stop",
    response_model=RunnerSessionStopResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("execute_flows")
def stop_runner_session(
    session_id: str,
    body: Optional[RunnerSessionStopRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RunnerSessionStopResponse:
    """Ask the runner to end the session (``graceful``) or kill the harness."""
    service, record, audit = _session_audit(db, current_user, session_id)
    mode = (body or RunnerSessionStopRequest()).mode
    if record.state in ACTIVE_STATES:
        current = record
        try:
            record = from_thread.run(lambda: service.stop(db, current, mode=mode))
        except RunnerSessionError as exc:
            raise audit.reject(409, exc.code, exc.message) from None
    audit.log(
        "runner_session.stop_requested",
        resource_id=record.remote_session_id,
        remote_session_id=record.remote_session_id,
        mode=mode,
    )
    return RunnerSessionStopResponse(session_id=record.session_id, state=record.state)
