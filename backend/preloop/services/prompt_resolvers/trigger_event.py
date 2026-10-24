"""Resolver for trigger event placeholders."""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from preloop.services.issue_references import (
    NO_REFERENCES,
    format_issue_references,
    references_from_trigger_payload,
)

from .base import PromptResolver, ResolverContext

logger = logging.getLogger(__name__)

# Same shape as ``extract_jira_issue_key_from_trigger``: a bare key, not a
# GitHub/GitLab issue number.
_JIRA_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,30}-[0-9]{1,10}$")
_CODE_HOST_SOURCES = {"github", "gitlab", "bitbucket", "bitbucket_dc"}


def _flatten_label_names(labels: Any) -> list[str]:
    """Return label names from objects or strings, preserving order."""
    names: list[str] = []
    if not isinstance(labels, list):
        return names
    for item in labels:
        if isinstance(item, dict):
            name = item.get("title") or item.get("name") or item.get("label")
            if name:
                names.append(str(name))
        elif isinstance(item, str) and item.strip():
            names.append(item)
    return names


def _enrich_object_attributes(payload: Dict[str, Any], attrs: Dict[str, Any]) -> None:
    """Copy labels and updated_at onto object_attributes when missing."""
    raw_labels = attrs.get("labels")
    if isinstance(raw_labels, list) and raw_labels and isinstance(raw_labels[0], dict):
        flattened = _flatten_label_names(raw_labels)
        if flattened:
            attrs["labels"] = flattened
    elif raw_labels is None:
        labels: list[str] = []
        if "labels" in payload:
            labels = _flatten_label_names(payload.get("labels"))
        else:
            issue = payload.get("issue")
            if isinstance(issue, dict) and "labels" in issue:
                labels = _flatten_label_names(issue.get("labels"))
        if labels or "labels" in payload:
            attrs["labels"] = labels
    if not attrs.get("updated_at"):
        issue = payload.get("issue")
        if isinstance(issue, dict) and issue.get("updated_at"):
            attrs["updated_at"] = issue.get("updated_at")


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


def _attach_close_reference(attrs: Dict[str, Any], source: str) -> None:
    """Set the token a pull request body uses to close the triggering issue.

    GitHub and GitLab close with ``#<number>``. Jira links a Bitbucket pull
    request when the body contains the bare issue key (``Closes KEY-4``). A
    leading hash stops that link.
    """
    if not isinstance(attrs, dict) or attrs.get("close_reference"):
        return
    number = attrs.get("number")
    if number is None:
        return
    text = str(number).strip()
    if not text:
        return
    if source == "jira" or _JIRA_ISSUE_KEY.fullmatch(text):
        attrs["close_reference"] = text
    else:
        attrs["close_reference"] = f"#{text}"


def _jira_description_text(description: Any) -> str:
    """Render a Jira description to plain text.

    Cloud webhooks send Atlassian Document Format. Server and some fields
    send a string. Both become the text the preset prompt reads.
    """
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        from preloop.sync.trackers.jira import JiraTracker

        return JiraTracker._adf_to_plain_text(description)
    return str(description)


def _jira_site_url(issue: Dict[str, Any]) -> str:
    """Origin of a Jira issue, taken from its REST self URL."""
    candidates = [issue.get("self")]
    fields = issue.get("fields")
    if isinstance(fields, dict):
        project = fields.get("project")
        if isinstance(project, dict):
            candidates.append(project.get("self"))
    for raw in candidates:
        if isinstance(raw, str) and "/rest/api/" in raw:
            return raw.split("/rest/api/", 1)[0].rstrip("/")
    return ""


def _jira_browse_url(issue: Dict[str, Any]) -> str:
    """``<site>/browse/<key>`` for a Jira issue webhook object."""
    key = str(issue.get("key") or "").strip()
    site = _jira_site_url(issue)
    if site and key:
        return f"{site}/browse/{key}"
    return ""


def _is_jira_issue_payload(source: str, payload: Dict[str, Any]) -> bool:
    """Whether ``payload.issue`` is a Jira issue, not a GitHub issue.

    GitHub issues carry ``title`` and ``number``. Jira issues carry ``key``
    and ``fields``. A ``source`` of ``jira`` wins even when fields are thin,
    so the GitHub mapper cannot write ``None`` into ``object_attributes``.
    """
    if source in _CODE_HOST_SOURCES:
        return False
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return False
    webhook_event = str(payload.get("webhookEvent") or "")
    if source == "jira" or webhook_event.startswith("jira:"):
        return True
    fields = issue.get("fields")
    key = str(issue.get("key") or "").strip()
    return (
        isinstance(fields, dict)
        and bool(key)
        and "title" not in issue
        and "number" not in issue
    )


def _jira_object_attributes(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Jira issue webhook object onto GitLab-shaped object_attributes."""
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    key = str(issue.get("key") or "").strip()
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    reporter = fields.get("reporter")
    if not isinstance(reporter, dict):
        creator = fields.get("creator")
        reporter = creator if isinstance(creator, dict) else {}
    labels = fields.get("labels")
    attrs: Dict[str, Any] = {
        "title": fields.get("summary") or "",
        "description": _jira_description_text(fields.get("description")),
        "url": _jira_browse_url(issue),
        "number": key,
        "iid": key,
        "state": status.get("name") or "",
        "author": reporter.get("displayName") or reporter.get("accountId") or "",
        "labels": _flatten_label_names(labels) if isinstance(labels, list) else [],
        "updated_at": fields.get("updated") or "",
    }
    _attach_close_reference(attrs, "jira")
    return attrs


def _bound_repository(event: Dict[str, Any], context: Any) -> Optional[Tuple[str, str]]:
    """Short name and path of the code host bound to this Jira trigger.

    Returns:
        ``(name, full_name)`` when a flow or project binding applies, else
        None. A binding that cannot be applied leaves the name unset; the
        orchestrator still fails the run when it applies the same binding.
    """
    if context is None or getattr(context, "db", None) is None:
        return None
    if not getattr(context, "flow_id", None):
        return None
    from sqlalchemy.exc import SQLAlchemyError

    from preloop.models.crud import crud_flow
    from preloop.services.repository_binding import (
        RepositoryBindingError,
        resolve_repository_binding,
    )

    try:
        flow = crud_flow.get(context.db, id=context.flow_id)
        if flow is None:
            return None
        account_id = getattr(context, "account_id", None) or event.get("account_id")
        if not account_id and getattr(flow, "account_id", None):
            account_id = str(flow.account_id)
        project_id = event.get("project_id")
        binding = resolve_repository_binding(
            context.db,
            account_id=str(account_id) if account_id else None,
            git_clone_config=getattr(flow, "git_clone_config", None),
            trigger_tracker_id=event.get("tracker_id"),
            trigger_source=event.get("source"),
            trigger_project_id=str(project_id) if project_id else None,
        )
    except RepositoryBindingError:
        logger.debug(
            "Jira trigger repository binding could not be applied", exc_info=True
        )
        return None
    except (SQLAlchemyError, TypeError, ValueError, AttributeError):
        logger.warning(
            "Could not resolve the bound repository for a Jira trigger",
            exc_info=True,
        )
        return None
    if binding is None:
        return None
    repo_path = str(getattr(binding, "repository", "") or "").strip()
    if not repo_path:
        return None
    return repo_path.rsplit("/", 1)[-1], repo_path


def _attach_bound_repository(event: Dict[str, Any], context: Any) -> None:
    """Copy the bound code-host repository onto a Jira trigger payload.

    Preset 011 reads ``payload.repository.name``. Jira webhooks have no
    repository of their own; the name comes from the flow or project binding.
    """
    if str(event.get("source") or "").lower() != "jira":
        return
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return
    repository = payload.get("repository")
    if isinstance(repository, dict) and repository.get("name"):
        return
    found = _bound_repository(event, context)
    if found is None:
        return
    name, full_name = found
    payload["repository"] = {"name": name, "full_name": full_name}


def _attach_referenced_issues(
    payload: Dict[str, Any], attrs: Dict[str, Any], source: str
) -> None:
    """Add ``object_attributes.referenced_issues`` for pull/merge requests.

    Neither GitHub nor GitLab hands the reviewer a linked-issue relation on
    the PR read, so the Pull Request Reviewer preset needs the references
    parsed off the PR body, title, and branch. Parsing here (deterministic,
    unit-tested) beats asking the model to run regexes in its head, and the
    resulting identifiers are exactly what the ``get_issue`` tool accepts.
    """
    if not isinstance(attrs, dict) or attrs.get("referenced_issues"):
        return
    is_pull_request = (
        "pull_request" in payload
        or payload.get("object_kind") == "merge_request"
        or bool(attrs.get("source_branch"))
    )
    if not is_pull_request:
        return
    try:
        refs = references_from_trigger_payload(payload, attrs, source)
        attrs["referenced_issues"] = format_issue_references(refs)
    except Exception:  # pragma: no cover - never fail prompt resolution
        logger.warning(
            "Failed to parse issue references from PR payload", exc_info=True
        )
        attrs["referenced_issues"] = NO_REFERENCES


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
        # Do not copy nested url. Real GitLab Note hooks already set
        # object_attributes.url to the note's own anchor, so a missing-url
        # guard never fires and overwriting would hide the comment that
        # triggered resume. The issue/MR URL stays on payload.issue.url or
        # payload.merge_request.url.
        break


class TriggerEventResolver(PromptResolver):
    """
    Resolver for trigger event data.

    Handles placeholders like:
    - {{trigger_event.payload.issue.title}}
    - {{trigger_event.payload.commit.sha}}
    - {{trigger_event.source}}
    - {{trigger_event.payload.object_attributes.title}} (normalized for GitHub, GitLab and Jira)
    """

    @property
    def prefix(self) -> str:
        """Return the prefix this resolver handles."""
        return "trigger_event"

    def _normalize_event_data(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize event data to provide a consistent structure.

        This adds an `object_attributes` field that maps to:
        - GitLab: payload.object_attributes (native)
        - GitHub: payload.pull_request or payload.issue (mapped)
        - Jira: payload.issue.fields (summary, description, key, browse URL)

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
                _enrich_object_attributes(payload, attrs)
                _attach_referenced_issues(payload, attrs, source)
                _attach_close_reference(attrs, source)
            return normalized

        # Jira issue webhooks carry ``issue.key`` and ``issue.fields``, not
        # GitHub's title/number. Map them before the GitHub branch, which
        # treats any ``issue`` key as a GitHub issue and would store None.
        if isinstance(payload, dict) and _is_jira_issue_payload(source, payload):
            payload["object_attributes"] = _jira_object_attributes(payload["issue"])
            normalized["payload"] = payload
            return normalized

        # Bitbucket Cloud: map the ``pullrequest`` object onto the same keys
        # so the reviewer preset prompt reads one shape for every provider.
        if isinstance(payload, dict) and isinstance(payload.get("pullrequest"), dict):
            from preloop.utils.bitbucket import build_object_attributes

            object_attributes = build_object_attributes(payload["pullrequest"])
            payload["object_attributes"] = object_attributes
            _attach_referenced_issues(payload, object_attributes, source)
            _attach_close_reference(object_attributes, source)
            normalized["payload"] = payload
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
                _attach_referenced_issues(payload, object_attributes, source)
                _attach_close_reference(object_attributes, source or "github")
                self.logger.debug(
                    f"Normalized GitHub PR to object_attributes: {object_attributes.get('title')}"
                )

            # Handle GitHub issue events
            elif "issue" in payload:
                issue = payload["issue"]
                user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
                object_attributes = {
                    "title": issue.get("title"),
                    "description": issue.get("body"),
                    "url": issue.get("html_url") or issue.get("url"),
                    "state": issue.get("state"),
                    "author": user.get("login") or user.get("username"),
                    "number": issue.get("number"),
                    "iid": issue.get("number"),
                    "labels": _flatten_label_names(issue.get("labels")),
                    "updated_at": issue.get("updated_at"),
                }
                payload["object_attributes"] = object_attributes
                _enrich_object_attributes(payload, object_attributes)
                _attach_close_reference(object_attributes, source or "github")
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

        The list is looked up with :func:`workspace_seed_payload`, the same
        inside-then-beside rule every other reader uses, so a body that puts
        ``workspace_files`` next to ``payload`` is redacted too.
        """
        from preloop.utils.workspace_seed import (
            WORKSPACE_FILES_KEY,
            WorkspaceSeedError,
            workspace_seed_payload,
        )

        def _redact_list(files: Any) -> list:
            redacted_files = []
            for entry in files:
                if isinstance(entry, dict) and isinstance(
                    entry.get("content_base64"), str
                ):
                    entry = dict(entry)
                    entry["content_base64"] = (
                        f"<{len(entry['content_base64'])} base64 chars "
                        "omitted; file is written under /workspace>"
                    )
                redacted_files.append(entry)
            return redacted_files

        containers: list[Any] = []
        try:
            containers.append(workspace_seed_payload(event_data))
        except WorkspaceSeedError:
            containers.extend((event_data, event_data.get("payload")))
        for container in containers:
            if not isinstance(container, dict):
                continue
            files = container.get(WORKSPACE_FILES_KEY)
            if isinstance(files, list):
                container[WORKSPACE_FILES_KEY] = _redact_list(files)
        return event_data

    async def resolve(self, path: str, context: ResolverContext) -> Any:
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
        # repository.name is not on a Jira webhook. Fill it from the bound
        # code host when the placeholder (or a full-event dump) asks for it.
        lookup = (path or "").strip()
        if lookup == "" or lookup.startswith("payload.repository"):
            _attach_bound_repository(normalized_data, context)

        # If no path specified, return entire event as JSON
        if not path or path.strip() == "":
            import json

            try:
                return json.dumps(normalized_data, indent=2)
            except Exception as e:
                self.logger.error(f"Failed to serialize trigger event data: {e}")
                return None

        # Handle direct event fields using normalized data
        raw = self._get_nested_raw(normalized_data, path)

        if raw is None:
            self.logger.debug(f"Could not resolve trigger_event.{path} in event data")
            return None
        # An object or list is returned as is, so the orchestrator can tell it
        # carries author-written fields and move it out of the template.
        if isinstance(raw, (dict, list)):
            return raw
        return str(raw)
