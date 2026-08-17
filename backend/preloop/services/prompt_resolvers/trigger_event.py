"""Resolver for trigger event placeholders."""

import logging
from typing import Any, Dict, Optional

from .base import PromptResolver, ResolverContext

logger = logging.getLogger(__name__)


class TriggerEventResolver(PromptResolver):
    """
    Resolver for trigger event data.

    Handles placeholders like:
    - {{trigger_event.payload.issue.title}}
    - {{trigger_event.payload.commit.sha}}
    - {{trigger_event.source}}
    - {{trigger_event.payload.object_attributes.title}} (normalized for both GitHub and GitLab)
    """

    @property
    def prefix(self) -> str:
        """Return the prefix this resolver handles."""
        return "trigger_event"

    def _normalize_event_data(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize event data to provide a consistent structure for GitHub and GitLab.

        This adds an `object_attributes` field that maps to:
        - GitLab: payload.object_attributes (native)
        - GitHub: payload.pull_request or payload.issue (mapped)

        Also adds platform-agnostic fields for common operations.
        """
        import copy

        # Deep copy to avoid mutating the original event_data
        normalized = copy.deepcopy(event_data)
        payload = normalized.get("payload", {})
        source = normalized.get("source", "").lower()

        # If object_attributes already exists (GitLab), keep it
        if "object_attributes" in payload:
            return normalized

        # For GitHub, create object_attributes from pull_request or issue
        if source == "github" or "pull_request" in payload or "issue" in payload:
            # Handle GitHub PR events
            if "pull_request" in payload:
                pr = payload["pull_request"]
                # Create GitLab-style object_attributes from GitHub PR
                object_attributes = {
                    "title": pr.get("title"),
                    "description": pr.get("body"),
                    "url": pr.get("html_url"),
                    "source_branch": pr.get("head", {}).get("ref"),
                    "target_branch": pr.get("base", {}).get("ref"),
                    "state": pr.get("state"),
                    "draft": pr.get("draft", False),
                    "author": pr.get("user", {}).get("login"),
                    "number": pr.get("number"),
                    "iid": pr.get("number"),  # GitLab uses iid
                }
                payload["object_attributes"] = object_attributes
                self.logger.debug(
                    f"Normalized GitHub PR to object_attributes: {object_attributes.get('title')}"
                )

            # Handle GitHub issue events
            elif "issue" in payload:
                issue = payload["issue"]
                object_attributes = {
                    "title": issue.get("title"),
                    "description": issue.get("body"),
                    "url": issue.get("html_url"),
                    "state": issue.get("state"),
                    "author": issue.get("user", {}).get("login"),
                    "number": issue.get("number"),
                    "iid": issue.get("number"),
                }
                payload["object_attributes"] = object_attributes
                self.logger.debug(
                    f"Normalized GitHub issue to object_attributes: {object_attributes.get('title')}"
                )

            normalized["payload"] = payload

        return normalized

    @staticmethod
    def _redact_workspace_files(event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace inline ``workspace_files`` blobs in full-event dumps.

        Those files are materialized under ``/workspace`` before the agent
        starts; embedding their base64 into the prompt is exactly what the
        feature exists to avoid. Applied to every resolution (full-event and
        payload embeds alike); paths and other entry fields still resolve.
        """
        payload = event_data.get("payload")
        if not isinstance(payload, dict):
            return event_data
        files = payload.get("workspace_files")
        if not isinstance(files, list):
            return event_data
        redacted_files = []
        for entry in files:
            if isinstance(entry, dict) and isinstance(entry.get("content_base64"), str):
                entry = dict(entry)
                entry["content_base64"] = (
                    f"<{len(entry['content_base64'])} base64 chars "
                    "omitted; file is written under /workspace>"
                )
            redacted_files.append(entry)
        # event_data is already a deep copy made by _normalize_event_data.
        payload["workspace_files"] = redacted_files
        return event_data

    async def resolve(self, path: str, context: ResolverContext) -> Optional[str]:
        """
        Resolve trigger event placeholders.

        Args:
            path: Path after the prefix (e.g., "payload.issue.title")
                  If empty, returns the entire trigger event as JSON
            context: Resolver context

        Returns:
            Resolved value or None
        """
        if not context.trigger_event_data:
            self.logger.warning("No trigger event data available")
            return None

        # Normalize event data to provide consistent structure, then strip
        # inline workspace_files blobs so they cannot be embedded in prompts.
        normalized_data = self._redact_workspace_files(
            self._normalize_event_data(context.trigger_event_data)
        )

        # If no path specified, return entire event as JSON
        if not path or path.strip() == "":
            import json

            try:
                return json.dumps(normalized_data, indent=2)
            except Exception as e:
                self.logger.error(f"Failed to serialize trigger event data: {e}")
                return None

        # Handle direct event fields using normalized data
        value = self._safe_get_nested(normalized_data, path)

        if value is None:
            self.logger.debug(f"Could not resolve trigger_event.{path} in event data")

        return value
