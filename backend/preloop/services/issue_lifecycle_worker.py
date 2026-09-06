"""Keep complete lifecycle ORM transactions away from the application loop."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial, wraps
from typing import Any, NamedTuple, ParamSpec, TypeVar
from uuid import UUID

import anyio
from anyio import from_thread
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

P = ParamSpec("P")
T = TypeVar("T")

_lifecycle_bind: ContextVar[Connection | None] = ContextVar(
    "lifecycle_bind", default=None
)


def _caller_session(*args: Any, **kwargs: Any) -> Session | None:
    """Find the request/test Session without treating test doubles as ORM."""
    for value in (*args, *kwargs.values()):
        if isinstance(value, Session):
            return value
        db = getattr(value, "db", None)
        if isinstance(db, Session):
            return db
    return None


def worker_owned_session(owner: Any = None, fallback: Any = None) -> tuple[Any, bool]:
    """Open a Session on this worker thread instead of borrowing the caller's.

    Engine-bound callers get a fresh factory Session whose commit is durable.
    Connection-bound callers (pytest savepoints) join that connection so
    uncommitted fixture rows stay visible. Test doubles are unchanged.
    """
    join = _lifecycle_bind.get()
    session = fallback if isinstance(fallback, Session) else None
    if session is None:
        db = getattr(owner, "db", None)
        if isinstance(db, Session):
            session = db
    if join is None and session is not None:
        raw = session.get_bind()
        if isinstance(raw, Connection):
            join = raw
    if join is not None:
        return (
            Session(bind=join, join_transaction_mode="rollback_only"),
            True,
        )
    if session is not None:
        from preloop.models.db.session import get_session_factory

        return get_session_factory()(), True
    return fallback, False


@contextmanager
def lifecycle_worker_db(owner: Any = None, fallback: Any = None) -> Iterator[Any]:
    """Open a worker-thread Session.

    Engine-bound callers get a durable factory Session. Connection-bound
    callers (pytest savepoints) join that connection so fixture rows stay
    visible without stealing the caller's savepoint.
    """
    db, owned = worker_owned_session(owner, fallback)
    joined = owned and isinstance(db.get_bind(), Connection)
    try:
        yield db
        if owned:
            if joined:
                db.flush()
            else:
                db.commit()
    except Exception:
        if owned and not joined:
            db.rollback()
        raise
    finally:
        if owned:
            db.close()


def run_lifecycle_endpoint(operation: Callable[[], Awaitable[T]]) -> T:
    """Run one HTTP operation inside its synchronous FastAPI worker handler.

    The private loop handles provider I/O while all synchronous CRUD and lazy
    ORM reads stay in the same worker. Detached flow tasks must be dispatched
    through ``on_application_loop``, never started on this temporary loop.
    """
    return anyio.run(operation)


LIFECYCLE_ENTRY_EVENTS = frozenset({"issue_labeled", "issue_updated", "issue_created"})
LIFECYCLE_ENTRY_KINDS = frozenset({"merge_audit", "refinement"})


class LifecycleEntry(NamedTuple):
    """One trigger decision: the flow kind, its tenant project and pickup."""

    kind: str | None
    project: Any | None
    pickup: bool

    @property
    def engaged(self) -> bool:
        """True when the entry hook reads or writes lifecycle state."""
        return self.project is not None and (
            self.kind in LIFECYCLE_ENTRY_KINDS or self.pickup
        )


def lifecycle_entry_decision(
    db: Any, flow: Any, event: dict[str, Any]
) -> LifecycleEntry:
    """Decide once whether a trigger event belongs to a lifecycle flow.

    The entry hook and the caller-commit gate both read this decision, so the
    gate cannot silently under-commit when the entry conditions change.
    """
    kind = (flow.agent_config or {}).get("lifecycle_kind")
    event_type = event.get("type")
    if kind not in LIFECYCLE_ENTRY_KINDS and event_type not in LIFECYCLE_ENTRY_EVENTS:
        return LifecycleEntry(kind, None, False)
    project_id = event.get("project_id")
    if not project_id or not flow.account_id:
        return LifecycleEntry(kind, None, False)
    from preloop.models.crud import crud_issue_lifecycle

    project = crud_issue_lifecycle.get_project(
        db, project_id=UUID(str(project_id)), account_id=flow.account_id
    )
    if project is None:
        return LifecycleEntry(kind, None, False)
    policy = (project.settings or {}).get("issue_lifecycle") or {}
    pickup = (
        event_type in LIFECYCLE_ENTRY_EVENTS
        and policy.get("ready_enabled") is True
        and str(policy.get("implementation_flow_id")) == str(flow.id)
    )
    return LifecycleEntry(kind, project, pickup)


def _should_commit_lifecycle_caller(caller: Session, *args: Any, **kwargs: Any) -> bool:
    """Commit only after the event is a confirmed lifecycle operation."""
    flow = None
    event: dict[str, Any] | None = None
    execution_event: dict[str, Any] | None = None
    for value in (*args, *kwargs.values()):
        config = getattr(value, "agent_config", None)
        if isinstance(config, dict) and getattr(value, "account_id", None) is not None:
            flow = value
        if isinstance(value, dict) and (
            "type" in value or "project_id" in value or "payload" in value
        ):
            event = value
        details = getattr(value, "trigger_event_details", None)
        if isinstance(details, dict):
            execution_event = details
    if execution_event is not None:
        envelope = (execution_event.get("payload") or {}).get(
            "lifecycle"
        ) or execution_event.get("lifecycle_refinement")
        return bool(envelope)
    if flow is None or event is None:
        return False
    return lifecycle_entry_decision(caller, flow, event).engaged


def lifecycle_worker_hook(
    operation: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    """Offload a trigger or completion hook with its entire ORM transaction."""

    @wraps(operation)
    async def offload(*args: P.args, **kwargs: P.kwargs) -> T:
        caller = _caller_session(*args, **kwargs)
        join = None
        if caller is not None:
            raw = caller.get_bind()
            if isinstance(raw, Connection):
                join = raw
            elif _should_commit_lifecycle_caller(caller, *args, **kwargs):
                caller.commit()
            else:
                # Ordinary triggers are not lifecycle work. Stay on the caller
                # thread instead of opening a worker loop and Session.
                return await operation(*args, **kwargs)
        token = _lifecycle_bind.set(join)
        try:
            return await anyio.to_thread.run_sync(
                partial(run_lifecycle_endpoint, partial(operation, *args, **kwargs))
            )
        finally:
            _lifecycle_bind.reset(token)
            if caller is not None:
                caller.expire_all()

    return offload


async def on_application_loop(operation: Callable[[], Awaitable[T]]) -> T:
    """Dispatch on the originating application loop, preserving detached tasks.

    Called only inside a lifecycle worker. The worker waits without using its
    session concurrently; the originating AnyIO token selects the persistent
    application loop, not the worker's temporary provider-I/O loop.
    """
    return from_thread.run(operation)


async def dispatch_lifecycle_execution(
    execution_id: UUID, local_dispatch: Callable[[], Awaitable[Any]]
) -> None:
    """Require a worker publish acknowledgment or schedule on the persistent loop."""
    from preloop.services.flow_execution_dispatcher import (
        dispatch_execute,
        flow_execution_worker_enabled,
    )
    from preloop.services.flow_trigger_service import FlowDispatchError

    async def dispatch() -> None:
        if flow_execution_worker_enabled():
            if not await dispatch_execute(execution_id):
                raise FlowDispatchError(
                    str(execution_id),
                    "PENDING",
                    RuntimeError("worker_dispatch_not_acknowledged"),
                )
        else:
            await local_dispatch()

    await on_application_loop(dispatch)
