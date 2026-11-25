"""Interactive issue lookups preserve safe provider status during initialization."""

from typing import Any
from unittest.mock import MagicMock

import gitlab
import pytest
from jira import JIRAError

from preloop.sync.exceptions import TrackerResponseError
from preloop.sync.trackers.factory import create_tracker_client


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gitlab", "jira"])
@pytest.mark.parametrize("status", [429, 503])
async def test_bounded_factory_preserves_provider_status(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    status: int,
) -> None:
    """Constructor failures carry status without embedding provider bodies."""
    constructor: Any
    if kind == "gitlab":
        constructor = MagicMock()
        constructor.return_value.auth.side_effect = gitlab.exceptions.GitlabError(
            "secret response", response_code=status
        )
        monkeypatch.setattr("preloop.sync.trackers.gitlab.gitlab.Gitlab", constructor)
    else:
        constructor = MagicMock(
            side_effect=JIRAError(status_code=status, text="secret response")
        )
        monkeypatch.setattr("preloop.sync.trackers.jira.JIRA", constructor)
    with pytest.raises(TrackerResponseError) as caught:
        await create_tracker_client(
            kind,
            "example-tracker",
            "test-token",
            {"url": f"https://{kind}.example.com", "username": "jane@example.com"},
            raise_on_error=True,
            request_timeout=0.1,
        )
    assert caught.value.status_code == status
    assert "secret" not in str(caught.value)
    assert constructor.call_args.kwargs["timeout"] == 0.1
