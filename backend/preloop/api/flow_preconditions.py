"""HTTP side of conditional flow writes and triggers (#2375).

Endpoints read ``If-Match`` with :func:`if_match_condition`, check it under
the flow's row lock with :func:`require_revision`, and stamp the resulting
``ETag`` with :func:`set_flow_etag`.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException, Response

from preloop.models.schemas.flow_revision import (
    FlowRevisionMismatchError,
    MalformedIfMatchError,
    RevisionCondition,
    flow_etag,
    parse_if_match,
)

PRECONDITION_FAILED_CODE = "flow_revision_mismatch"

IF_MATCH_DESCRIPTION = (
    "Optional precondition: the flow's ETag (its revision in quotes, for "
    'example "7") or *. A stale value is refused with 412 and nothing is '
    "changed. Without the header the request is unconditional."
)

CONDITIONAL_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    400: {"description": "The If-Match header is malformed."},
    412: {
        "description": (
            "If-Match named another revision; nothing was changed. The body "
            "names current_revision and the ETag header carries it: re-read "
            "the flow and reconsider the change."
        )
    },
}


def if_match_condition(value: Any) -> Optional[RevisionCondition]:
    """Parse the request's ``If-Match`` header.

    Args:
        value: The header value. Anything other than a string (no header,
            or a direct call that left FastAPI's default in place) means no
            precondition.

    Returns:
        The condition, or None for an unconditional request.

    Raises:
        HTTPException: 400 when the header is malformed.
    """
    if not isinstance(value, str):
        return None
    try:
        return parse_if_match(value)
    except MalformedIfMatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def precondition_failed(current_revision: int) -> HTTPException:
    """Build the 412 response for a stale precondition.

    Args:
        current_revision: The flow's revision now.

    Returns:
        An HTTPException carrying the current revision and ETag.
    """
    return HTTPException(
        status_code=412,
        detail={
            "code": PRECONDITION_FAILED_CODE,
            "message": (
                "The flow changed since it was read. Re-read it, reconsider "
                "the change, and send the new ETag in If-Match."
            ),
            "current_revision": current_revision,
        },
        headers={"ETag": flow_etag(current_revision)},
    )


def require_revision(condition: Optional[RevisionCondition], revision: int) -> None:
    """Refuse the request unless the precondition holds.

    Call it while holding the flow's row lock, before changing anything.

    Args:
        condition: The parsed precondition, or None for none.
        revision: The flow's current revision.

    Raises:
        HTTPException: 412 when the condition names another revision.
    """
    if condition is None:
        return
    try:
        condition.require(revision)
    except FlowRevisionMismatchError as exc:
        raise precondition_failed(exc.current_revision) from None


def require_flow_revision(condition: Optional[RevisionCondition], flow: Any) -> None:
    """Refuse the request unless the precondition holds for ``flow``.

    The flow's revision is read only when there is a condition, so an
    unconditional request behaves exactly as before.

    Args:
        condition: The parsed precondition, or None for none.
        flow: The locked flow row.

    Raises:
        HTTPException: 412 when the condition names another revision.
    """
    if condition is not None:
        require_revision(condition, flow.config_revision)


def set_flow_etag(response: Any, revision: Any) -> None:
    """Set the ``ETag`` header for a flow revision on a response.

    Args:
        response: The injected response; ignored when not a Response (a
            direct call from code rather than a request).
        revision: The flow's revision; anything but an int (an unloaded or
            stand-in value) leaves the header unset.
    """
    if isinstance(response, Response) and isinstance(revision, int):
        response.headers["ETag"] = flow_etag(revision)
