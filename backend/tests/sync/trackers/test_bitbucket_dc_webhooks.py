"""Repository hook registration and inspection on Bitbucket Data Center 10.2."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from preloop.sync.exceptions import TrackerPermissionError
from preloop.sync.trackers.bitbucket_dc import CAPABILITIES
from preloop.utils.bitbucket_dc_webhooks import BITBUCKET_DC_WEBHOOK_EVENTS
from tests.sync.trackers.test_bitbucket_dc import (  # noqa: F401 - fixture
    REPO_PATH,
    dc_env,
    make_tracker,
    ok,
    page,
)

pytestmark = pytest.mark.asyncio

CALLBACK = "https://preloop.example.com/api/v1/private/webhooks/bitbucket_dc/t-1"
HOOKS = f"{REPO_PATH}/webhooks"


class FakeHooks:
    """In-memory hook list served over the REST shape."""

    def __init__(self, hooks: List[Dict[str, Any]], status: int = 200) -> None:
        self.hooks = hooks
        self.status = status
        self.writes: List[tuple[str, str, Dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return ok({"errors": [{"message": "You are not permitted"}]}, self.status)
        path = request.url.path
        if request.method == "GET" and path == HOOKS:
            return ok(page(self.hooks))
        body = json.loads(request.content or b"{}")
        if request.method == "POST" and path == HOOKS:
            hook = {**body, "id": 100 + len(self.hooks)}
            self.hooks.append(hook)
            self.writes.append(("POST", path, body))
            return ok(hook, 201)
        if request.method == "PUT" and path.startswith(f"{HOOKS}/"):
            hook_id = int(path.rsplit("/", 1)[1])
            for hook in self.hooks:
                if hook["id"] == hook_id:
                    hook.update(body)
            self.writes.append(("PUT", path, body))
            return ok({**body, "id": hook_id})
        return ok({"errors": [{"message": "unrouted"}]}, 404)


def unrelated_hook() -> Dict[str, Any]:
    return {
        "id": 7,
        "name": "CI",
        "url": "https://ci.example.com/hook",
        "active": True,
        "events": ["repo:refs_changed"],
        "configuration": {},
    }


async def test_capability_is_advertised() -> None:
    assert CAPABILITIES["webhooks"] is True


async def test_registration_creates_once_and_rotation_updates_in_place() -> None:
    fake = FakeHooks([unrelated_hook()])
    tracker = make_tracker(fake, [])
    first = await tracker.ensure_repository_webhook(CALLBACK, "secret-1")
    assert first["created"] is True
    second = await tracker.ensure_repository_webhook(CALLBACK, "secret-2")
    assert second == {"id": first["id"], "created": False, "updated": 1}
    ours = [h for h in fake.hooks if h["url"] == CALLBACK]
    assert len(ours) == 1
    assert ours[0]["configuration"] == {"secret": "secret-2"}
    assert ours[0]["events"] == list(BITBUCKET_DC_WEBHOOK_EVENTS)
    # The unrelated hook is never written, let alone deleted.
    assert fake.hooks[0] == unrelated_hook()
    assert all(path != f"{HOOKS}/7" for _, path, _ in fake.writes)
    assert [m for m, _, _ in fake.writes] == ["POST", "PUT"]


async def test_registration_without_repo_admin_raises_permission_error() -> None:
    tracker = make_tracker(FakeHooks([], status=403), [])
    with pytest.raises(TrackerPermissionError):
        await tracker.ensure_repository_webhook(CALLBACK, "secret")


async def test_inspection_reports_each_state_distinctly() -> None:
    assert (await make_tracker(FakeHooks([]), []).inspect_repository_webhook(CALLBACK))[
        "status"
    ] == "missing"
    denied = make_tracker(FakeHooks([], status=403), [])
    assert (await denied.inspect_repository_webhook(CALLBACK))["status"] == (
        "permission_denied"
    )
    unauthorized = make_tracker(FakeHooks([], status=401), [])
    assert (await unauthorized.inspect_repository_webhook(CALLBACK))["status"] == (
        "unauthorized"
    )
    partial = {
        "id": 9,
        "url": CALLBACK,
        "active": True,
        "events": ["pr:opened"],
    }
    state = await make_tracker(FakeHooks([partial]), []).inspect_repository_webhook(
        CALLBACK
    )
    assert state["status"] == "events_missing"
    assert "pr:comment:added" in state["missing_events"]
    full = {**partial, "events": list(BITBUCKET_DC_WEBHOOK_EVENTS)}
    tracker = make_tracker(FakeHooks([full]), [])
    assert (await tracker.inspect_repository_webhook(CALLBACK))["status"] == (
        "registered"
    )
    inactive = {**full, "active": False}
    tracker = make_tracker(FakeHooks([inactive]), [])
    assert (await tracker.inspect_repository_webhook(CALLBACK))["status"] == (
        "inactive"
    )


async def test_unregister_all_never_deletes_hooks() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(FakeHooks([unrelated_hook()]), requests)
    assert await tracker.unregister_all_webhooks(None) == {  # type: ignore[arg-type]
        "unregistered": 0,
        "failed": 0,
        "not_found": 0,
    }
    assert requests == []
