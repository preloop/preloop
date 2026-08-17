"""Tests for trigger event resolver."""

from unittest.mock import MagicMock

import pytest

from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver


class TestTriggerEventResolver:
    """Test TriggerEventResolver class."""

    def test_prefix(self):
        """Test that prefix returns correct value."""
        resolver = TriggerEventResolver()
        assert resolver.prefix == "trigger_event"

    @pytest.mark.asyncio
    async def test_resolve_simple_field(self):
        """Test resolving a simple field from trigger event."""
        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"source": "github", "action": "created"},
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("source", context)
        assert result == "github"

        result = await resolver.resolve("action", context)
        assert result == "created"

    @pytest.mark.asyncio
    async def test_resolve_nested_field(self):
        """Test resolving a nested field from trigger event."""
        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={
                "payload": {
                    "issue": {"title": "Test Issue", "number": 123},
                    "commit": {"sha": "abc123"},
                }
            },
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("payload.issue.title", context)
        assert result == "Test Issue"

        result = await resolver.resolve("payload.commit.sha", context)
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_resolve_no_trigger_event_data(self):
        """Test resolving when no trigger event data is available."""
        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data=None,
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("source", context)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_field(self):
        """Test resolving a field that doesn't exist."""
        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"source": "github"},
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("nonexistent.field", context)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_empty_trigger_event_data(self):
        """Test resolving when trigger event data is empty."""
        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={},
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("source", context)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_entire_event(self):
        """Test resolving the entire trigger event as JSON when no path is provided.

        Note: The resolver normalizes GitHub events by adding object_attributes
        for cross-platform compatibility with GitLab.
        """
        import json

        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        trigger_data = {
            "source": "github",
            "action": "opened",
            "payload": {
                "issue": {"title": "Test Issue", "number": 123},
                "repository": {"name": "test-repo"},
            },
        }
        context = ResolverContext(
            db=mock_db,
            trigger_event_data=trigger_data,
            flow_id="flow-1",
            execution_id="exec-1",
        )

        # Test with empty string path
        result = await resolver.resolve("", context)
        assert result is not None
        parsed = json.loads(result)

        # The resolver normalizes GitHub events by adding object_attributes
        assert parsed["source"] == "github"
        assert parsed["action"] == "opened"
        assert parsed["payload"]["issue"] == {"title": "Test Issue", "number": 123}
        assert parsed["payload"]["repository"] == {"name": "test-repo"}
        # Normalized object_attributes should be present for GitHub issue events
        assert "object_attributes" in parsed["payload"]
        assert parsed["payload"]["object_attributes"]["title"] == "Test Issue"
        assert parsed["payload"]["object_attributes"]["number"] == 123

        # Test with whitespace-only path
        result = await resolver.resolve("  ", context)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["source"] == "github"
        assert "object_attributes" in parsed["payload"]

    @pytest.mark.asyncio
    async def test_resolve_entire_event_complex_structure(self):
        """Test resolving entire event with complex nested structure."""
        import json

        resolver = TriggerEventResolver()

        mock_db = MagicMock()
        trigger_data = {
            "tracker_type": "gitlab",
            "event_type": "merge_request",
            "timestamp": "2025-11-05T12:00:00Z",
            "payload": {
                "object_attributes": {
                    "id": 999,
                    "title": "Add new feature",
                    "state": "opened",
                    "source_branch": "feature/new",
                    "target_branch": "main",
                },
                "user": {"name": "John Doe", "email": "john@example.com"},
                "labels": [
                    {"name": "enhancement", "color": "#00FF00"},
                    {"name": "priority:high", "color": "#FF0000"},
                ],
            },
        }
        context = ResolverContext(
            db=mock_db,
            trigger_event_data=trigger_data,
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("", context)
        assert result is not None
        parsed = json.loads(result)
        assert parsed == trigger_data
        # Verify nested structures are preserved
        assert parsed["payload"]["object_attributes"]["title"] == "Add new feature"
        assert len(parsed["payload"]["labels"]) == 2


class TestWorkspaceFilesRedaction:
    """workspace_files blobs must never be embedded into prompts."""

    @pytest.mark.asyncio
    async def test_full_event_dump_redacts_content(self):
        """{{trigger_event}} omits inline file contents but keeps paths."""
        import base64
        import json

        resolver = TriggerEventResolver()
        content = base64.b64encode(b"fixture-bytes" * 100).decode("ascii")
        context = ResolverContext(
            db=MagicMock(),
            trigger_event_data={
                "source": "webhook",
                "payload": {
                    "run": "smoke-1",
                    "workspace_files": [
                        {"path": "fixtures/input.json", "content_base64": content}
                    ],
                },
            },
            flow_id="flow-1",
            execution_id="exec-1",
        )

        result = await resolver.resolve("", context)
        assert content not in result
        parsed = json.loads(result)
        entry = parsed["payload"]["workspace_files"][0]
        assert entry["path"] == "fixtures/input.json"
        assert "omitted" in entry["content_base64"]
        # The rest of the payload still resolves normally.
        assert parsed["payload"]["run"] == "smoke-1"

    @pytest.mark.asyncio
    async def test_payload_fields_still_resolve(self):
        """Deep paths next to workspace_files are unaffected."""
        resolver = TriggerEventResolver()
        context = ResolverContext(
            db=MagicMock(),
            trigger_event_data={
                "payload": {
                    "run": "smoke-1",
                    "workspace_files": [{"path": "a.txt", "content_base64": "eA=="}],
                }
            },
            flow_id="flow-1",
            execution_id="exec-1",
        )
        assert await resolver.resolve("payload.run", context) == "smoke-1"
