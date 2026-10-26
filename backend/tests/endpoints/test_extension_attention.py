"""Generic plugin inbox contributions are authorized before providers execute."""

from datetime import UTC, datetime
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from preloop.api.endpoints.account import list_extension_attention_items
from preloop.plugins.attention import AttentionSource
from preloop.plugins.base import Plugin, PluginManager, PluginMetadata
from preloop.schemas.attention import ExtensionAttentionItem


def item(**changes: object) -> ExtensionAttentionItem:
    """Return a synthetic contribution."""
    return ExtensionAttentionItem.model_validate(
        dict(
            id="one",
            source="ignored",
            title="Review configuration",
            detail="Observed change",
            href="/console/settings/example",
            severity="action",
            opened_at=datetime.now(UTC),
        )
        | changes
    )


@pytest.mark.asyncio
async def test_no_sources_returns_empty_without_database_reads() -> None:
    manager = PluginManager()
    db = MagicMock()
    with patch("preloop.plugins.base.get_plugin_manager", return_value=manager):
        assert await list_extension_attention_items(SimpleNamespace(), db) == []
    db.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.asyncio
async def test_shared_read_includes_pending_reports_for_flow_viewers() -> None:
    """Report proposals use the existing extension read with their evidence."""
    account_id = uuid4()
    report_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id, is_superuser=False)
    started_at = datetime(2026, 10, 26, 4, 0)
    execution = SimpleNamespace(
        id=report_id,
        start_time=started_at,
        result={"follow_ups": [{"title": "Review tests"}]},
    )
    manager = MagicMock()
    manager.attention_sources.return_value = []
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.api.endpoints.account.crud_account.get", return_value=None),
        patch(
            "preloop.api.auth.permissions.has_permission",
            side_effect=lambda _user, permission, _db: permission == "view_flows",
        ),
        patch(
            "preloop.services.delivery_report_views.crud_flow_execution.delivery_reports_with_proposals",
            return_value=[execution],
        ) as candidates,
    ):
        items = await list_extension_attention_items(user, MagicMock(), True)
    candidates.assert_called_once()
    assert len(items) == 1
    assert items[0].source == "core:delivery_report"
    assert items[0].delivery_report is not None
    assert items[0].delivery_report.started_at == started_at.replace(tzinfo=UTC)
    assert items[0].opened_at == started_at.replace(tzinfo=UTC)
    assert items[0].delivery_report.proposals[0].title == "Review tests"
    assert items[0].href == f"/console/delivery-reports/{report_id}#needs-you"


@pytest.mark.asyncio
async def test_shared_read_excludes_reports_without_flow_permission() -> None:
    """The shared endpoint applies the report endpoint's read permission."""
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), is_superuser=False)
    manager = MagicMock()
    manager.attention_sources.return_value = []
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.api.endpoints.account.crud_account.get", return_value=None),
        patch("preloop.api.auth.permissions.has_permission", return_value=False),
        patch(
            "preloop.services.delivery_report_views.crud_flow_execution.delivery_reports_with_proposals"
        ) as candidates,
    ):
        assert await list_extension_attention_items(user, MagicMock(), True) == []
    candidates.assert_not_called()


@pytest.mark.asyncio
async def test_permission_filter_namespace_dedupe_and_failure_isolation() -> None:
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), is_superuser=False)
    permitted = AsyncMock(return_value=[item(), item()])
    forbidden = AsyncMock(return_value=[item()])
    broken = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    manager = MagicMock()
    manager.attention_sources.return_value = [
        ("example", AttentionSource("one", "view_example", permitted)),
        ("example", AttentionSource("private", "private", forbidden)),
        ("example", AttentionSource("broken", "view_example", broken)),
    ]
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.api.endpoints.account.crud_account.get", return_value=None),
        patch(
            "preloop.api.auth.permissions.has_permission",
            side_effect=lambda u, p, db: p == "view_example",
        ),
    ):
        result = await list_extension_attention_items(user, MagicMock())
    assert len(result) == 1
    assert result[0].source == "example:one"
    permitted.assert_awaited_once()
    assert permitted.call_args.args[1] is user
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_credentials_cannot_read_sources() -> None:
    user = SimpleNamespace(
        _auth_api_key=SimpleNamespace(context_data={"device_report": {}})
    )
    with pytest.raises(HTTPException) as exc:
        await list_extension_attention_items(user, MagicMock())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_timeout_cancels_provider_rolls_back_and_continues() -> None:
    cancelled = False

    async def slow(db: object, user: object) -> list[ExtensionAttentionItem]:
        nonlocal cancelled
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return []

    good = AsyncMock(return_value=[item()])
    manager = MagicMock()
    manager.attention_sources.return_value = [
        ("example", AttentionSource("slow", "view_example", slow)),
        ("example", AttentionSource("good", "view_example", good)),
    ]
    db = MagicMock()
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), is_superuser=True)
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.plugins.attention.ATTENTION_SOURCE_TIMEOUT_SECONDS", 0.01),
        patch("preloop.api.endpoints.account.crud_account.get", return_value=None),
    ):
        result = await list_extension_attention_items(user, db)
    assert cancelled
    assert len(result) == 1 and result[0].source == "example:good"
    db.rollback.assert_called_once()
    good.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversized_namespace_is_isolated_without_breaking_valid_source() -> None:
    manager = MagicMock()
    manager.attention_sources.return_value = [
        (
            "x" * 255,
            AttentionSource(
                "oversized", "view_example", AsyncMock(return_value=[item()])
            ),
        ),
        (
            "example",
            AttentionSource("good", "view_example", AsyncMock(return_value=[item()])),
        ),
    ]
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), is_superuser=True)
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.api.endpoints.account.crud_account.get", return_value=None),
    ):
        result = await list_extension_attention_items(user, MagicMock())
    assert len(result) == 1 and result[0].source == "example:good"


@pytest.mark.parametrize(
    "href",
    ["https://example.com", "//example.com", "/console\\evil", "/console/\ninvalid"],
)
def test_destinations_must_be_local_console_paths(href: str) -> None:
    with pytest.raises(ValidationError):
        item(href=href)


def test_plugin_hook_defaults_and_manager_registration() -> None:
    class Example(Plugin):
        @property
        def metadata(self) -> PluginMetadata:
            return PluginMetadata("example", "1", "Example", "Example")

    manager = PluginManager()
    manager.register_plugin(Example())
    assert manager.attention_sources() == []
