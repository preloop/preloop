"""Code host adapters for the backport flow (issue #961).

The runner in :mod:`preloop.services.backport` speaks to one small protocol,
:class:`BackportHost`, so a host is added by writing one adapter. GitHub and
GitLab ship here. Bitbucket Cloud follows issue #955: it needs an adapter over
that tracker client and a ``bitbucket`` entry in :func:`backport_host_for`,
and nothing in the runner changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Protocol

BACKPORT_HOST_TYPES = ("github", "gitlab")


class BackportHostError(RuntimeError):
    """A host API call failed. The message is safe to store and show."""


@dataclass(frozen=True)
class HostChange:
    """One pull request (GitHub) or merge request (GitLab).

    Attributes:
        number: Number (GitHub) or iid (GitLab).
        url: Web URL.
        state: ``open``, ``closed``, ``merged`` or a host specific state.
    """

    number: int
    url: str
    state: str = "open"


def safe_host_error(action: str, error: BaseException) -> str:
    """Describe a host failure without echoing the response body.

    Host error messages can quote request data. The status code is enough to
    act on and never carries a credential.
    """
    status = getattr(error, "status_code", None)
    if status:
        return f"{action} failed with HTTP {status}"
    return f"{action} failed ({type(error).__name__})"


class BackportHost(Protocol):
    """What the backport runner needs from a code host."""

    kind: str
    git_auth_username: str

    async def find_change(self, branch: str, target: str) -> Optional[HostChange]:
        """Newest change in any state from ``branch`` into ``target``."""

    async def open_change(
        self, branch: str, target: str, title: str, body: str
    ) -> HostChange:
        """Open a change. Never merges it."""

    async def update_change(self, number: int, title: str, body: str) -> HostChange:
        """Refresh the title and description of an open change."""

    async def request_reviewers(self, number: int, reviewers: List[str]) -> None:
        """Ask ``reviewers`` to review. Raises BackportHostError on failure."""

    async def comment_on_original(self, number: int, body: str) -> None:
        """Post one comment on the original change."""


def _pick_newest(items: List[dict[str, Any]]) -> Optional[HostChange]:
    """Prefer an open change, then the newest one in any state."""
    if not items:
        return None
    ordered = sorted(items, key=lambda item: item.get("state") != "open")
    item = ordered[0]
    return HostChange(
        number=int(item.get("number") or item.get("iid") or 0),
        url=str(item.get("url") or ""),
        state=str(item.get("state") or "open"),
    )


class GitHubBackportHost:
    """GitHub adapter over :class:`preloop.sync.trackers.github.GitHubTracker`."""

    kind = "github"
    git_auth_username = "x-access-token"

    def __init__(self, tracker: Any) -> None:
        """Wrap a GitHub tracker client bound to the repository."""
        self._tracker = tracker

    async def find_change(self, branch: str, target: str) -> Optional[HostChange]:
        """Newest pull request from ``branch`` into ``target``."""
        try:
            items = await self._tracker.find_pull_requests_by_branch(branch, target)
        except Exception as error:  # noqa: BLE001 - reported, never raised raw
            raise BackportHostError(
                safe_host_error("Looking up the backport pull request", error)
            ) from error
        return _pick_newest(items)

    async def open_change(
        self, branch: str, target: str, title: str, body: str
    ) -> HostChange:
        """Open a pull request. Reviewers are requested separately."""
        try:
            created = await self._tracker.create_pull_request(
                title=title,
                source_branch=branch,
                target_branch=target,
                description=body,
            )
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Opening the backport pull request", error)
            ) from error
        return HostChange(number=int(created["number"]), url=str(created["url"]))

    async def update_change(self, number: int, title: str, body: str) -> HostChange:
        """Refresh the title and body. Reviewers are left untouched."""
        try:
            updated = await self._tracker.update_pull_request(
                str(number), title=title, description=body
            )
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Updating the backport pull request", error)
            ) from error
        return HostChange(
            number=int(updated.get("number") or number),
            url=str(updated.get("url") or ""),
        )

    async def request_reviewers(self, number: int, reviewers: List[str]) -> None:
        """Request reviews, raising a safe error on failure."""
        try:
            await self._tracker.request_pull_request_reviewers(number, reviewers)
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Requesting reviewers", error)
            ) from error

    async def comment_on_original(self, number: int, body: str) -> None:
        """Comment on the original pull request (the issues comment API)."""
        try:
            await self._tracker.add_comment(str(number), body)
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Commenting on the original pull request", error)
            ) from error


class GitLabBackportHost:
    """GitLab adapter over :class:`preloop.sync.trackers.gitlab.GitLabTracker`."""

    kind = "gitlab"
    git_auth_username = "oauth2"

    def __init__(self, tracker: Any) -> None:
        """Wrap a GitLab tracker client bound to the project."""
        self._tracker = tracker

    async def find_change(self, branch: str, target: str) -> Optional[HostChange]:
        """Newest merge request from ``branch`` into ``target``."""
        try:
            items = await self._tracker.find_merge_requests_by_branch(branch, target)
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Looking up the backport merge request", error)
            ) from error
        return _pick_newest(items)

    async def open_change(
        self, branch: str, target: str, title: str, body: str
    ) -> HostChange:
        """Open a merge request. Reviewers are requested separately."""
        try:
            created = await self._tracker.create_merge_request(
                title=title,
                source_branch=branch,
                target_branch=target,
                description=body,
            )
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Opening the backport merge request", error)
            ) from error
        return HostChange(number=int(created["iid"]), url=str(created["url"]))

    async def update_change(self, number: int, title: str, body: str) -> HostChange:
        """Refresh the title and description. Reviewers are left untouched."""
        try:
            updated = await self._tracker.update_merge_request(
                str(number), title=title, description=body
            )
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Updating the backport merge request", error)
            ) from error
        return HostChange(
            number=int(updated.get("iid") or number),
            url=str(updated.get("url") or ""),
        )

    async def request_reviewers(self, number: int, reviewers: List[str]) -> None:
        """Resolve usernames and set them as reviewers of a new merge request.

        GitLab replaces the reviewer list, which is safe here because this is
        only called for a merge request the runner has just opened. A name
        that does not resolve is a failure, even when others did.
        """
        try:
            ids = [
                await self._tracker.get_user_id_by_username(name) for name in reviewers
            ]
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Looking up reviewers", error)
            ) from error
        found = [user_id for user_id in ids if user_id is not None]
        missing = [
            name
            for name, user_id in zip(reviewers, ids, strict=True)
            if user_id is None
        ]
        if found:
            try:
                await self._tracker.update_merge_request(
                    str(number), reviewer_ids=found
                )
            except Exception as error:  # noqa: BLE001
                raise BackportHostError(
                    safe_host_error("Requesting reviewers", error)
                ) from error
        if missing:
            raise BackportHostError(
                "Reviewers not found on GitLab: " + ", ".join(sorted(missing))
            )

    async def comment_on_original(self, number: int, body: str) -> None:
        """Start one discussion on the original merge request."""
        try:
            await self._tracker.create_mr_discussion(str(number), body)
        except Exception as error:  # noqa: BLE001
            raise BackportHostError(
                safe_host_error("Commenting on the original merge request", error)
            ) from error


def backport_host_for(tracker_type: str, tracker: Any) -> BackportHost:
    """Adapter for a tracker client.

    Args:
        tracker_type: The tracker's ``tracker_type`` value.
        tracker: A tracker client bound to the repository.

    Returns:
        The adapter.

    Raises:
        BackportHostError: The host has no backport support yet.
    """
    kind = (tracker_type or "").lower()
    if kind == "github":
        return GitHubBackportHost(tracker)
    if kind == "gitlab":
        return GitLabBackportHost(tracker)
    raise BackportHostError(
        f"Backports are not supported for {kind or 'this'} trackers yet. "
        "Bitbucket support follows issue #955"
    )


__all__ = [
    "BACKPORT_HOST_TYPES",
    "BackportHost",
    "BackportHostError",
    "GitHubBackportHost",
    "GitLabBackportHost",
    "HostChange",
    "backport_host_for",
    "safe_host_error",
]
