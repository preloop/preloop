"""Regression tests for the local gateway-usage benchmark harness.

The harness is not collected as a unit test itself (``scripts/`` is in
``norecursedirs``), but its helper functions are still worth pinning where a
bug would silently leave seeded rows behind.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.perf import benchmark_gateway_usage as benchmark


class _RecordingQuery:
    """Minimal query stub that records the bulk ``api_usage`` delete."""

    def __init__(self) -> None:
        self.deleted = False

    def filter(self, *args: Any, **kwargs: Any) -> "_RecordingQuery":
        return self

    def first(self) -> Any:
        return MagicMock()

    def delete(self, *args: Any, **kwargs: Any) -> int:
        self.deleted = True
        return 0


def test_cleanup_uses_the_account_crud_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cleanup`` must delete through an existing CRUD method.

    ``CRUDAccount`` exposes ``purge``, not ``remove``; the earlier
    implementation raised ``AttributeError`` after deleting the usage rows, so
    the seeded account (and its owner user, sessions and models) survived a
    cleanup run.
    """
    session = MagicMock()
    query = _RecordingQuery()
    session.query.return_value = query

    purged: dict[str, Any] = {}

    def fake_purge(db: Any, *, account_id: str) -> None:
        purged["db"] = db
        purged["account_id"] = account_id

    monkeypatch.setattr(benchmark.crud_account, "purge", fake_purge)

    benchmark._cleanup(session, "perf-account")

    assert query.deleted is True
    assert purged == {"db": session, "account_id": "perf-account"}
