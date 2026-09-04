"""Resolver for trigger event placeholders."""

import logging
from typing import Any, Dict, Optional

from .base import PromptResolver, ResolverContext

logger = logging.getLogger(__name__)


def _alias_object_attribute_ids(attrs: Dict[str, Any]) -> None:
    """Expose both GitHub ``number`` and GitLab ``iid`` on object_attributes.

    Presets (including Automated Issue Implementation) use
    ``object_attributes.number`` so ``Closes #N`` resolves. GitHub issue and
    pull-request payloads already set both keys when mapped into
    object_attributes. GitLab native object_attributes only have ``iid``.
    """
    if not isinstance(attrs, dict):
        return
    number = attrs.get("number")
    iid = attrs.get("iid")
    if number is None and iid is not None:
        attrs["number"] = iid
    if iid is None and number is not None:
        attrs["iid"] = number


def _lift_gitlab_noteable_ids(payload: Dict[str, Any], attrs: Dict[str, Any]) -> None:
    """Copy the issue/MR iid off a GitLab Note hook onto object_attributes.

    Note hooks put the note in ``object_attributes`` (no iid) and the
    issue or merge request beside it. Without this, resume prompts leave
    ``{{trigger_event.payload.object_attributes.number}}`` unresolved.
    """
    if attrs.get("number") is not None or attrs.get("iid") is not None:
        return
    for key in ("issue", "merge_request"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        iid = nested.get("iid")
        if iid is None:
            continue
        attrs["number"] = iid
        attrs["iid"] = iid
        if not attrs.get("title") and nested.get("title"):
            attrs["title"] = nested["title"]
        if not attrs.get("description") and nested.get("description"):
            attrs["description"] = nested["description"]
        if not attrs.get("url") and nested.get("url"):
            attrs["url"] = nested["url"]
        break


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

        # GitLab (and other trackers that already ship object_attributes):
        # keep the native object but alias number/iid and lift Note-hook
        # issue/MR identifiers so the same placeholders resolve everywhere.
        if "object_attributes" in payload:
            attrs = payload.get("object_attributes")
            if isinstance(attrs, dict):
                _lift_gitlab_noteable_ids(payload, attrs)
                _alias_object_attribute_ids(attrs)
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
