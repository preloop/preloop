"""Preserve provider statuses while sanitizing GitLab credential failures."""

from unittest.mock import Mock

import gitlab
import pytest
import requests

from preloop.sync.exceptions import (
    TrackerConnectionError,
    TrackerPermissionError,
    TrackerResponseError,
)
from preloop.sync.trackers.gitlab import GitLabTracker
from preloop.sync.trackers.utils import is_not_found_error


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_no_retry_operation_errors_keep_status(status: int) -> None:
    """Webhook permission errors are typed; 404 still enables existing fallback."""
    tracker = GitLabTracker("test", "token", {}, initialize_client=False)
    method = Mock(
        side_effect=gitlab.exceptions.GitlabListError(
            "provider detail", response_code=status
        )
    )
    with pytest.raises(TrackerResponseError) as caught:
        await tracker._make_request_no_retry(method)
    assert caught.value.status_code == status
    if status == 403:
        assert isinstance(caught.value, TrackerPermissionError)
        assert "api scope" in str(caught.value)
        assert "provider detail" not in str(caught.value)
    else:
        assert is_not_found_error(caught.value)


@pytest.mark.asyncio
async def test_no_retry_transport_failure_is_sanitized() -> None:
    """Provider messages may contain connection details and must not be exposed."""
    tracker = GitLabTracker("test", "token", {}, initialize_client=False)
    method = Mock(
        side_effect=requests.exceptions.ChunkedEncodingError("private detail")
    )
    with pytest.raises(TrackerConnectionError) as caught:
        await tracker._make_request_no_retry(method)
    assert "private detail" not in str(caught.value)
