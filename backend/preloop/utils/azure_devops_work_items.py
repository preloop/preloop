"""Azure Boards work items as Preloop issues: pure mapping helpers.

Nothing here touches the network or the database. The tracker client
(``preloop.sync.trackers.azure_devops``) and the webhook intake
(``preloop.api.endpoints.webhooks``) share these functions so a work item
read over REST and one delivered by a service hook produce the same shape.

Contract, from Microsoft's documentation:

* Work items: ``GET /{org}/_apis/wit/workitems/{id}?$expand=relations``,
  ``POST /{org}/{project}/_apis/wit/workitems/${type}`` and
  ``PATCH /{org}/_apis/wit/workitems/{id}`` with a JSON Patch body
  (``application/json-patch+json``)
  (https://learn.microsoft.com/rest/api/azure/devops/wit/work-items).
* Search: ``POST /{org}/{project}/_apis/wit/wiql`` returns ids only
  (https://learn.microsoft.com/rest/api/azure/devops/wit/wiql/query-by-wiql);
  the items are then read in batches of at most 200 with
  ``POST /{org}/_apis/wit/workitemsbatch``
  (https://learn.microsoft.com/rest/api/azure/devops/wit/work-items/get-work-items-batch).
* Comments: ``/{org}/{project}/_apis/wit/workItems/{id}/comments`` with
  ``api-version=7.1-preview.4``
  (https://learn.microsoft.com/rest/api/azure/devops/wit/comments).
* States carry a category (``Proposed``, ``InProgress``, ``Resolved``,
  ``Completed``, ``Removed``) whatever their display name is in a process
  template or language: ``GET /{org}/{project}/_apis/wit/workitemtypes/
  {type}/states``
  (https://learn.microsoft.com/rest/api/azure/devops/wit/work-item-type-states).
* Service hooks ``workitem.created``, ``workitem.updated`` and
  ``workitem.commented`` (https://learn.microsoft.com/azure/devops/
  service-hooks/events#work-item-created). ``workitem.updated`` carries the
  changed ``fields`` as ``{"oldValue", "newValue"}`` pairs and the full
  ``revision``; the other two carry the work item itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from preloop.utils.azure_devops import (
    AZURE_DEVOPS_BASE_URL,
    AZURE_DEVOPS_HOST,
    WORK_ITEM_EVENT_MAP,
)
from preloop.utils.azure_devops_html import html_to_markdown

# Comments are still a preview API in 7.1.
COMMENTS_API_VERSION = "7.1-preview.4"
WORK_ITEMS_BATCH_LIMIT = 200

# State categories that mean "closed" in Preloop. ``Resolved`` (Agile,
# CMMI) is still open: the item awaits verification.
CLOSED_STATE_CATEGORIES = frozenset({"Completed", "Removed"})


FIELD_TITLE = "System.Title"
FIELD_DESCRIPTION = "System.Description"
FIELD_STATE = "System.State"
FIELD_TYPE = "System.WorkItemType"
FIELD_ASSIGNED_TO = "System.AssignedTo"
FIELD_TAGS = "System.Tags"
FIELD_PROJECT = "System.TeamProject"
FIELD_PRIORITY = "Microsoft.VSTS.Common.Priority"
FIELD_CREATED_DATE = "System.CreatedDate"
FIELD_CREATED_BY = "System.CreatedBy"
FIELD_CHANGED_DATE = "System.ChangedDate"
FIELD_CHANGED_BY = "System.ChangedBy"
FIELD_CLOSED_DATE = "Microsoft.VSTS.Common.ClosedDate"
FIELD_HISTORY = "System.History"

# Preloop relation type -> Azure Boards link type
# (https://learn.microsoft.com/azure/devops/boards/queries/link-type-reference).
RELATION_LINK_TYPES: Dict[str, str] = {
    "relates_to": "System.LinkTypes.Related",
    "parent": "System.LinkTypes.Hierarchy-Reverse",
    "child": "System.LinkTypes.Hierarchy-Forward",
    "blocks": "System.LinkTypes.Dependency-Forward",
    "blocked_by": "System.LinkTypes.Dependency-Reverse",
    "duplicates": "System.LinkTypes.Duplicate-Reverse",
    "duplicated_by": "System.LinkTypes.Duplicate-Forward",
}
LINK_TYPE_RELATIONS: Dict[str, str] = {v: k for k, v in RELATION_LINK_TYPES.items()}
ARTIFACT_LINK = "ArtifactLink"
PULL_REQUEST_ARTIFACT_NAME = "Pull Request"

# Priority 1 (highest) .. 4 (lowest) on Agile, Scrum, CMMI and Basic.
PRIORITY_NAMES: Dict[int, str] = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}

REASON_UNSUPPORTED_EVENT = "unsupported_event"
REASON_NO_REPOSITORY = "no_project_for_work_item"


class WorkItemRejectedError(ValueError):
    """A work item delivery that must not be processed.

    Attributes:
        status_code: HTTP status to answer with.
        reason: Fixed, sanitized reason code.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        """Store the status and reason.

        Args:
            status_code: HTTP status to answer with.
            reason: Fixed reason code; never payload content.
        """
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass
class WorkItemDelivery:
    """Outcome of normalizing one authenticated work item delivery.

    Attributes:
        accepted: True when the delivery should be dispatched.
        event_key: The service hook event id.
        reason: Sanitized reason when acknowledged without dispatch.
        event_type: Normalized Preloop event type.
        payload: Normalized payload handed to the trigger pipeline.
        work_item_id: The work item id.
        revision: Revision the delivery describes.
        project_name: Azure DevOps project of the work item.
        changed_by_id: Identity id of whoever made the change.
    """

    accepted: bool
    event_key: str
    reason: Optional[str] = None
    event_type: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    work_item_id: Optional[int] = None
    revision: Optional[int] = None
    project_name: Optional[str] = None
    changed_by_id: Optional[str] = None


# ----------------------------------------------------------------------
# URLs and identifiers
# ----------------------------------------------------------------------


def work_item_web_url(organization: str, project: str, work_item_id: int) -> str:
    """Web URL of a work item."""
    return (
        f"{AZURE_DEVOPS_BASE_URL}/{quote(organization, safe='')}/"
        f"{quote(project, safe='')}/_workitems/edit/{int(work_item_id)}"
    )


def parse_work_item_url(url: str) -> Optional[Tuple[str, str, int]]:
    """Parse ``https://dev.azure.com/{org}/{project}/_workitems/edit/{id}``.

    Args:
        url: Candidate URL.

    Returns:
        ``(organization, project, id)``, or None for any other URL.
    """
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower().rstrip(".") != AZURE_DEVOPS_HOST:
        return None
    if parts.scheme != "https":
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if len(segments) != 5 or segments[2:4] != ["_workitems", "edit"]:
        return None
    if not segments[4].isdigit():
        return None
    return segments[0], segments[1], int(segments[4])


def parse_work_item_id(value: Any) -> Optional[int]:
    """Work item id from ``123``, ``#123``, ``AB#123``, an issue key
    (``org/project#123``) or a work item URL.

    Args:
        value: Identifier as given by a caller.

    Returns:
        The id, or None when ``value`` names no work item.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    parsed = parse_work_item_url(text)
    if parsed:
        return parsed[2]
    if "#" in text:
        # "org/project#123", "AB#123" or "#123".
        text = text.rsplit("#", 1)[1]
    return int(text) if text.isdigit() and int(text) > 0 else None


def work_item_key(organization: str, project: str, work_item_id: int) -> str:
    """Preloop issue key of a work item: ``org/project#id``.

    Mirrors GitHub's ``owner/repo#n``; the id alone is unique in the
    organization, the prefix keeps keys of two organizations apart.
    """
    return f"{organization}/{project}#{int(work_item_id)}"


def pull_request_artifact_uri(project_id: str, repository_id: str, pr_id: int) -> str:
    """``vstfs`` artifact URI of a pull request, for a work item link."""
    return f"vstfs:///Git/PullRequestId/{project_id}%2F{repository_id}%2F{int(pr_id)}"


# ----------------------------------------------------------------------
# Field mapping
# ----------------------------------------------------------------------


def parse_tags(value: Any) -> List[str]:
    """``System.Tags`` (``"a; b"``) as a list, order kept, blanks dropped."""
    if not isinstance(value, str):
        return []
    return [tag.strip() for tag in value.split(";") if tag.strip()]


def format_tags(labels: Iterable[str]) -> str:
    """A label list as ``System.Tags`` text. Semicolons cannot be escaped."""
    seen: List[str] = []
    for label in labels:
        text = str(label).replace(";", ",").strip()
        if text and text.casefold() not in {s.casefold() for s in seen}:
            seen.append(text)
    return "; ".join(seen)


def parse_datetime(value: Any) -> Optional[datetime]:
    """Azure timestamp (``2026-01-02T03:04:05.67Z``) as naive UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def identity(value: Any) -> Optional[Dict[str, Optional[str]]]:
    """``{"id", "name", "email"}`` from an ``IdentityRef`` or legacy string.

    Older payloads (and ``oldValue`` entries) may carry identities as
    ``"Jane Doe <jane@example.com>"``.
    """
    if isinstance(value, Mapping):
        name = value.get("displayName") or value.get("uniqueName")
        if not name:
            return None
        return {
            "id": str(value.get("id") or value.get("uniqueName") or name),
            "name": str(name),
            "email": value.get("uniqueName"),
        }
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if "<" in text and text.endswith(">"):
            name, email = text[:-1].split("<", 1)
            return {"id": email.strip(), "name": name.strip(), "email": email.strip()}
        return {"id": text, "name": text, "email": None}
    return None


def description_markdown(work_item: Mapping[str, Any]) -> str:
    """``System.Description`` as markdown.

    A field stored in Markdown format (``multilineFieldsFormat``) is passed
    through; HTML is converted.
    """
    fields = work_item.get("fields") or {}
    raw = fields.get(FIELD_DESCRIPTION)
    if not isinstance(raw, str):
        return ""
    formats = work_item.get("multilineFieldsFormat") or {}
    if str(formats.get(FIELD_DESCRIPTION) or "").lower() == "markdown":
        return raw.strip()
    return html_to_markdown(raw)


def priority_of(fields: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"id", "name", "level"}`` when the work item type has a priority."""
    value = fields.get(FIELD_PRIORITY)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = int(value)
    return {
        "id": str(level),
        "name": PRIORITY_NAMES.get(level, str(level)),
        "level": level,
    }


def parse_priority(value: Any) -> Optional[int]:
    """Priority 1-4 from ``"2"``, ``2`` or a name such as ``"High"``."""
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    by_name = {name.casefold(): level for level, name in PRIORITY_NAMES.items()}
    aliases = {"critical": 1, "urgent": 1, "normal": 3, "lowest": 4}
    return by_name.get(text.casefold()) or aliases.get(text.casefold())


def relations_of(work_item: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Work item links as ``{"relation_type", "id", "url"}``.

    Pull request artifact links are returned with relation type
    ``pull_request`` and the artifact URI instead of an id.
    """
    out: List[Dict[str, Any]] = []
    for rel in work_item.get("relations") or []:
        if not isinstance(rel, Mapping):
            continue
        rel_type = str(rel.get("rel") or "")
        url = str(rel.get("url") or "")
        if rel_type in LINK_TYPE_RELATIONS:
            target = url.rstrip("/").rsplit("/", 1)[-1]
            if target.isdigit():
                out.append(
                    {
                        "relation_type": LINK_TYPE_RELATIONS[rel_type],
                        "id": int(target),
                        "url": url,
                    }
                )
        elif rel_type == ARTIFACT_LINK and url.startswith(
            "vstfs:///Git/PullRequestId/"
        ):
            out.append({"relation_type": "pull_request", "id": None, "url": url})
    return out


def state_is_closed(category: Optional[str], state: Optional[str]) -> bool:
    """True when the state category means closed.

    Without a known category (the states lookup failed) only the
    ``ClosedDate`` rule in :func:`issue_status` applies, so no state name is
    ever matched as English text.
    """
    return bool(category) and category in CLOSED_STATE_CATEGORIES


def issue_status(
    fields: Mapping[str, Any], category: Optional[str]
) -> Dict[str, Optional[str]]:
    """``{"id", "name", "category", "state"}`` for a work item.

    ``state`` is ``open`` or ``closed``. When the state category is unknown,
    a set ``Microsoft.VSTS.Common.ClosedDate`` (which Azure fills when an
    item enters a completed state) decides.
    """
    state = fields.get(FIELD_STATE)
    name = str(state) if state is not None else ""
    if category:
        closed = state_is_closed(category, name)
    else:
        closed = bool(fields.get(FIELD_CLOSED_DATE))
    return {
        "id": name,
        "name": name,
        "category": category,
        "state": "closed" if closed else "open",
    }


def work_item_summary(
    work_item: Mapping[str, Any],
    organization: str,
    *,
    state_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Provider-neutral view of a work item.

    Args:
        work_item: A ``WorkItem`` from REST or a service hook resource.
        organization: The tracker's organization.
        state_category: Category of ``System.State``, when known.

    Returns:
        ``id``, ``key``, ``number``, ``title``, ``body`` (markdown),
        ``state``, ``state_name``, ``state_category``, ``work_item_type``,
        ``labels``, ``assignee``, ``author``, ``priority``, ``parent_id``,
        ``relations``, ``project``, ``revision``, ``html_url`` and times.
    """
    fields = work_item.get("fields") or {}
    work_item_id = int(work_item.get("id") or 0)
    project = str(fields.get(FIELD_PROJECT) or "")
    status = issue_status(fields, state_category)
    relations = relations_of(work_item)
    parent = next((r["id"] for r in relations if r["relation_type"] == "parent"), None)
    if parent is None and isinstance(fields.get("System.Parent"), int):
        parent = int(fields["System.Parent"])
    return {
        "id": work_item_id,
        "key": (
            work_item_key(organization, project, work_item_id)
            if project
            else str(work_item_id)
        ),
        "number": work_item_id,
        "title": str(fields.get(FIELD_TITLE) or ""),
        "body": description_markdown(work_item),
        "state": status["state"],
        "state_name": status["name"],
        "state_category": state_category,
        "work_item_type": fields.get(FIELD_TYPE),
        "labels": parse_tags(fields.get(FIELD_TAGS)),
        "assignee": identity(fields.get(FIELD_ASSIGNED_TO)),
        "author": identity(fields.get(FIELD_CREATED_BY)),
        "changed_by": identity(fields.get(FIELD_CHANGED_BY)),
        "priority": priority_of(fields),
        "parent_id": parent,
        "relations": relations,
        "project": project or None,
        "revision": work_item.get("rev"),
        "html_url": (
            work_item_web_url(organization, project, work_item_id)
            if project and work_item_id
            else None
        ),
        "created_at": fields.get(FIELD_CREATED_DATE),
        "updated_at": fields.get(FIELD_CHANGED_DATE),
        "closed_at": fields.get(FIELD_CLOSED_DATE),
    }


def comment_summary(comment: Mapping[str, Any]) -> Dict[str, Any]:
    """Provider-neutral view of a work item ``Comment``.

    Returns:
        ``id``, ``body`` (markdown), ``author``, ``created_at``,
        ``updated_at`` and ``url``.
    """
    text = comment.get("text")
    fmt = str(comment.get("format") or "html").lower()
    body = (text or "").strip() if fmt == "markdown" else html_to_markdown(text)
    return {
        "id": comment.get("id"),
        "body": body,
        "author": identity(comment.get("createdBy")),
        "created_at": comment.get("createdDate"),
        "updated_at": comment.get("modifiedDate") or comment.get("createdDate"),
        "url": comment.get("url"),
    }


# ----------------------------------------------------------------------
# JSON Patch
# ----------------------------------------------------------------------


def patch_ops(
    *,
    title: Optional[str] = None,
    description_html: Optional[str] = None,
    state: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[Iterable[str]] = None,
    priority: Optional[int] = None,
    extra_fields: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """JSON Patch operations for the given field changes.

    ``add`` sets a scalar field (an empty string clears it; ``remove``
    fails on a field that is already empty). Tags use ``replace``, because
    ``add`` merges them.
    """
    ops: List[Dict[str, Any]] = []

    def put(name: str, value: Any) -> None:
        ops.append({"op": "add", "path": f"/fields/{name}", "value": value})

    if title is not None:
        put(FIELD_TITLE, title)
    if description_html is not None:
        put(FIELD_DESCRIPTION, description_html)
    if state is not None:
        put(FIELD_STATE, state)
    if assignee is not None:
        put(FIELD_ASSIGNED_TO, assignee)
    if labels is not None:
        # ``add`` on System.Tags merges with the existing tags; ``replace``
        # sets the list (and clears it with an empty string).
        ops.append(
            {
                "op": "replace",
                "path": f"/fields/{FIELD_TAGS}",
                "value": format_tags(labels),
            }
        )
    if priority is not None:
        put(FIELD_PRIORITY, int(priority))
    for name, value in (extra_fields or {}).items():
        if isinstance(name, str) and "." in name and "/" not in name:
            put(name, value)
    return ops


# ----------------------------------------------------------------------
# Service hook deliveries
# ----------------------------------------------------------------------


def _new_values(fields: Any) -> Dict[str, Any]:
    """``newValue`` of each changed field in a ``workitem.updated`` body."""
    if not isinstance(fields, Mapping):
        return {}
    return {
        name: change.get("newValue")
        for name, change in fields.items()
        if isinstance(change, Mapping) and "newValue" in change
    }


def delivered_work_item(event_key: str, resource: Mapping[str, Any]) -> Dict[str, Any]:
    """The work item a delivery describes, in REST ``WorkItem`` shape.

    ``workitem.updated`` sends a ``WorkItemUpdate`` whose ``revision`` is the
    full item; the id of the update itself is not the work item id.
    """
    if event_key == "workitem.updated":
        revision = resource.get("revision")
        if isinstance(revision, Mapping) and revision.get("id"):
            return dict(revision)
        return {
            "id": resource.get("workItemId"),
            "rev": resource.get("rev"),
            "fields": _new_values(resource.get("fields")),
        }
    return dict(resource)


def changed_fields(event_key: str, resource: Mapping[str, Any]) -> Dict[str, Any]:
    """``{field: {"old", "new"}}`` for a ``workitem.updated`` delivery."""
    if event_key != "workitem.updated":
        return {}
    out: Dict[str, Any] = {}
    for name, change in (resource.get("fields") or {}).items():
        if isinstance(change, Mapping):
            out[name] = {"old": change.get("oldValue"), "new": change.get("newValue")}
    return out


def normalize_work_item_delivery(
    payload: Mapping[str, Any],
    *,
    organization: str,
    own_identity_ids: Iterable[str] = (),
) -> WorkItemDelivery:
    """Validate a work item delivery and map it to the issue event shape.

    Args:
        payload: Parsed service hook ``Event`` body.
        organization: The tracker's organization.
        own_identity_ids: Identity id and unique name of the token owner. A
            change made by that identity is Preloop's own write; it is
            marked ``azure_devops.self_generated`` so the flow loop guard
            does not let a flow trigger itself.

    Returns:
        The delivery outcome.

    Raises:
        WorkItemRejectedError: For malformed payloads or another
            organization's work item.
    """
    key = str(payload.get("eventType") or "").strip()
    event_type = WORK_ITEM_EVENT_MAP.get(key)
    if event_type is None:
        return WorkItemDelivery(
            accepted=False, event_key=key, reason=REASON_UNSUPPORTED_EVENT
        )
    resource = payload.get("resource")
    if not isinstance(resource, Mapping):
        raise WorkItemRejectedError(400, "missing_resource")
    item = delivered_work_item(key, resource)
    work_item_id = item.get("id")
    if isinstance(work_item_id, bool) or not isinstance(work_item_id, int):
        raise WorkItemRejectedError(400, "missing_work_item")
    for url in (resource.get("url"), item.get("url")):
        owner = _organization_of(url)
        if owner is not None and owner.casefold() != organization.casefold():
            raise WorkItemRejectedError(403, "organization_mismatch")
    fields = item.get("fields") or {}
    if not isinstance(fields, Mapping) or not fields.get(FIELD_PROJECT):
        raise WorkItemRejectedError(400, "missing_project")
    summary = work_item_summary(item, organization)
    revision = resource.get("rev") if key == "workitem.updated" else item.get("rev")
    if not isinstance(revision, int):
        revision = item.get("rev") if isinstance(item.get("rev"), int) else None

    changer: Optional[Dict[str, Optional[str]]]
    if key == "workitem.updated":
        changer = identity(resource.get("revisedBy")) or summary["changed_by"]
    elif key == "workitem.commented":
        changer = summary["changed_by"]
    else:
        changer = summary["author"]
    changed_by_id = (changer or {}).get("id")
    changer_keys = {
        str(v).casefold()
        for v in ((changer or {}).get("id"), (changer or {}).get("email"))
        if v
    }

    comment: Optional[Dict[str, Any]] = None
    if key == "workitem.commented":
        # The comment text arrives in System.History; Microsoft's sample
        # body has no comment id.
        history = fields.get(FIELD_HISTORY)
        comment = {
            "id": None,
            "body": html_to_markdown(history) if isinstance(history, str) else "",
            "author": changer,
            "created_at": fields.get(FIELD_CHANGED_DATE),
        }

    issue = {**summary, "user": {"login": (summary["author"] or {}).get("name")}}
    normalized: Dict[str, Any] = {
        "issue": issue,
        "object_attributes": {
            "title": summary["title"],
            "description": summary["body"],
            "url": summary["html_url"],
            "state": summary["state"],
            "iid": work_item_id,
            "number": work_item_id,
            "labels": summary["labels"],
        },
        "changes": changed_fields(key, resource),
        "sender": {"login": (changer or {}).get("name")},
        "azure_devops": {
            "organization": organization,
            "project": summary["project"],
            "work_item_id": work_item_id,
            "work_item_type": summary["work_item_type"],
            "revision": revision,
            "state": summary["state_name"],
        },
        "event_key": key,
        "delivery": {
            "id": payload.get("id"),
            "notification_id": payload.get("notificationId"),
            "subscription_id": payload.get("subscriptionId"),
            "created_date": payload.get("createdDate"),
        },
    }
    if comment is not None:
        # commentVersionRef names the comment (same id as the comments REST
        # API); older bodies lack it, and the revision identifies it then.
        ref = resource.get("commentVersionRef")
        ref_id = ref.get("commentId") if isinstance(ref, Mapping) else None
        comment["id"] = str(ref_id) if ref_id else f"rev:{revision}"
        normalized["comment"] = comment
    outcome = WorkItemDelivery(
        accepted=True,
        event_key=key,
        event_type=event_type,
        payload=normalized,
        work_item_id=work_item_id,
        revision=revision,
        project_name=summary["project"],
        changed_by_id=changed_by_id,
    )
    own = {str(i).casefold() for i in own_identity_ids if i}
    if changer_keys & own:
        # Still stored and dispatched: FlowTriggerService drops it unless
        # the event type is exempt from the loop guard (label hand-offs).
        normalized["azure_devops"]["self_generated"] = True
    return outcome


def states_snapshot(
    project: str, states: Mapping[str, List[Mapping[str, str]]]
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """``{project: {type: {state: category}}}`` (all casefolded).

    Stored on the tracker when hooks are registered, so the intake can tell
    open from closed without a network call.
    """
    return {
        project.casefold(): {
            wi_type: {
                str(st.get("name") or "").casefold(): str(st.get("category") or "")
                for st in type_states
            }
            for wi_type, type_states in states.items()
        }
    }


def state_category_from_snapshot(
    snapshot: Any, project: Optional[str], work_item_type: Any, state: Any
) -> Optional[str]:
    """Category of a state from :func:`states_snapshot` output, if known."""
    if not (isinstance(snapshot, Mapping) and project and work_item_type and state):
        return None
    types = snapshot.get(project.casefold())
    if not isinstance(types, Mapping):
        return None
    states = types.get(str(work_item_type).casefold())
    if not isinstance(states, Mapping):
        return None
    category = states.get(str(state).casefold())
    return str(category) if category else None


def apply_state_category(payload: Dict[str, Any], category: str) -> None:
    """Set the open/closed state of a normalized payload from a category."""
    state = "closed" if category in CLOSED_STATE_CATEGORIES else "open"
    issue = payload.get("issue")
    if isinstance(issue, dict):
        issue["state"] = state
        issue["state_category"] = category
    attrs = payload.get("object_attributes")
    if isinstance(attrs, dict):
        attrs["state"] = state


def tag_delta(changes: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(added, removed)`` tags from :func:`changed_fields` output."""
    change = changes.get(FIELD_TAGS) if isinstance(changes, Mapping) else None
    if not isinstance(change, Mapping):
        return [], []
    old = parse_tags(change.get("old"))
    new = parse_tags(change.get("new"))
    old_keys = {t.casefold() for t in old}
    new_keys = {t.casefold() for t in new}
    return (
        [t for t in new if t.casefold() not in old_keys],
        [t for t in old if t.casefold() not in new_keys],
    )


def state_change(changes: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """``(from, to)`` state names when the update moved the state."""
    change = changes.get(FIELD_STATE) if isinstance(changes, Mapping) else None
    if not isinstance(change, Mapping) or change.get("new") is None:
        return None
    old, new = change.get("old"), change.get("new")
    if old == new:
        return None
    return str(old or ""), str(new)


def work_item_delivery_identity(tracker_id: Any, delivery: WorkItemDelivery) -> str:
    """Tracker-scoped identity of one work item revision.

    Azure DevOps retries a delivery and may send ``workitem.updated`` and
    ``workitem.commented`` for the same revision; each revision is
    dispatched once per event type.
    """
    return (
        f"azure_devops:{tracker_id}:wi:{delivery.work_item_id}"
        f":rev:{delivery.revision}:{delivery.event_key}"
    )


def _organization_of(url: Any) -> Optional[str]:
    """Organization named by a ``dev.azure.com`` REST URL."""
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".visualstudio.com"):
        return host.split(".", 1)[0]
    if host != AZURE_DEVOPS_HOST:
        return None
    segments = [s for s in parts.path.split("/") if s]
    return unquote(segments[0]) if segments else None


# ----------------------------------------------------------------------
# WIQL and scope
# ----------------------------------------------------------------------

# Requirement-level type per process: Basic, Agile, Scrum, CMMI.
DEFAULT_WORK_ITEM_TYPES: Tuple[str, ...] = (
    "Issue",
    "User Story",
    "Product Backlog Item",
    "Requirement",
    "Task",
)


def wiql_literal(value: Any) -> str:
    """A WIQL string literal; single quotes are doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _state_clause(
    wanted: Iterable[str], states: Mapping[str, List[Mapping[str, str]]]
) -> Optional[str]:
    """WIQL condition for ``open`` / ``closed`` / state names.

    ``open`` and ``closed`` become, per work item type, the list of state
    names in (or not in) a closed category, so no English state name is
    assumed.
    """
    names: List[str] = []
    clauses: List[str] = []
    for item in wanted:
        text = str(item).strip()
        if text.casefold() not in {"open", "closed"}:
            if text:
                names.append(text)
            continue
        want_closed = text.casefold() == "closed"
        for wi_type, type_states in states.items():
            closed = [
                st["name"]
                for st in type_states
                if st.get("category") in CLOSED_STATE_CATEGORIES
            ]
            type_name = wi_type
            if not closed:
                if not want_closed:
                    clauses.append(f"[System.WorkItemType] = {wiql_literal(type_name)}")
                continue
            op = "IN" if want_closed else "NOT IN"
            listed = ", ".join(wiql_literal(n) for n in closed)
            clauses.append(
                f"([System.WorkItemType] = {wiql_literal(type_name)} "
                f"AND [System.State] {op} ({listed}))"
            )
    if names:
        clauses.append(
            f"[System.State] IN ({', '.join(wiql_literal(n) for n in names)})"
        )
    if not clauses:
        return None
    return "(" + " OR ".join(clauses) + ")"


def build_wiql(
    project: str,
    filters: Any,
    *,
    states: Mapping[str, List[Mapping[str, str]]],
) -> str:
    """WIQL selecting the ids of matching work items, newest change first.

    Args:
        project: Azure DevOps project name.
        filters: An ``IssueFilter``.
        states: Per work item type (casefolded) the ``{"name", "category"}``
            entries of its states, used to express ``open`` / ``closed``.

    Returns:
        The query text. Every value is passed as a quoted literal.
    """
    conditions = [f"[System.TeamProject] = {wiql_literal(project)}"]
    query = getattr(filters, "query", None)
    if query:
        literal = wiql_literal(query)
        conditions.append(
            f"([System.Title] CONTAINS {literal} OR "
            f"[System.Description] CONTAINS {literal})"
        )
    status = getattr(filters, "status", None)
    if status:
        clause = _state_clause(status, states)
        if clause:
            conditions.append(clause)
    for label in getattr(filters, "labels", None) or []:
        conditions.append(f"[System.Tags] CONTAINS {wiql_literal(label)}")
    for attr, name, op in (
        ("created_after", FIELD_CREATED_DATE, ">="),
        ("created_before", FIELD_CREATED_DATE, "<"),
        ("updated_after", FIELD_CHANGED_DATE, ">="),
        ("updated_before", FIELD_CHANGED_DATE, "<"),
    ):
        value = getattr(filters, attr, None)
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            stamp = value.strftime("%Y-%m-%dT%H:%M:%SZ")
            conditions.append(f"[{name}] {op} {wiql_literal(stamp)}")
    assigned = getattr(filters, "assigned_to", None)
    if assigned:
        value = (
            "@Me"
            if str(assigned).casefold() in {"@me", "me"}
            else wiql_literal(assigned)
        )
        conditions.append(f"[{FIELD_ASSIGNED_TO}] = {value}")
    reporter = getattr(filters, "reported_by", None)
    if reporter:
        conditions.append(f"[{FIELD_CREATED_BY}] = {wiql_literal(reporter)}")
    sort = {
        "created": FIELD_CREATED_DATE,
        "created_at": FIELD_CREATED_DATE,
        "title": FIELD_TITLE,
        "priority": FIELD_PRIORITY,
    }.get(str(getattr(filters, "sort_by", "") or "").casefold(), FIELD_CHANGED_DATE)
    direction = (
        "ASC"
        if str(getattr(filters, "sort_direction", "") or "").casefold() == "asc"
        else "DESC"
    )
    return (
        "SELECT [System.Id] FROM WorkItems WHERE "
        + " AND ".join(conditions)
        + f" ORDER BY [{sort}] {direction}"
    )


def work_item_home(
    board_project: str,
    repositories: Iterable[str],
    *,
    bound_repository: Optional[str] = None,
    overrides: Any = None,
) -> Optional[str]:
    """Repository whose Preloop project receives a project's work items.

    Work items belong to an Azure DevOps project, Preloop projects to a
    repository. In order:

    1. ``connection_details["work_item_repositories"][project]``;
    2. the repository the tracker is bound to;
    3. the project's only repository;
    4. the repository named like the project (Azure's default repository).

    Args:
        board_project: Azure DevOps project name.
        repositories: Repository names of that project known to Preloop.
        bound_repository: The tracker's bound repository, if any.
        overrides: ``{project name: repository name}``.

    Returns:
        The repository name, or None when the project has several
        repositories and none of the rules picks one.
    """
    names = [str(r) for r in repositories if r]
    by_key = {n.casefold(): n for n in names}
    if isinstance(overrides, Mapping):
        for project, repo in overrides.items():
            if str(project).casefold() == board_project.casefold() and repo:
                return by_key.get(str(repo).casefold())
    if bound_repository:
        return by_key.get(bound_repository.casefold())
    if len(names) == 1:
        return names[0]
    return by_key.get(board_project.casefold())


# connection_details keys Preloop derives from the token at registration.
DERIVED_IDENTITY_KEYS = ("token_identity_id", "token_identity_unique_name")
DERIVED_KEYS = (*DERIVED_IDENTITY_KEYS, "work_item_states")


def carry_derived_details(
    previous: Optional[Mapping[str, Any]],
    requested: Mapping[str, Any],
    *,
    token_changed: bool,
) -> Dict[str, Any]:
    """Keep registration-derived keys across a tracker edit.

    An edit sends the user-facing details only. The recorded token identity
    (loop guard) and state categories survive it; the identity is dropped
    when the token changes, because a new token may belong to another user
    (register again to record it).

    Args:
        previous: The stored connection details.
        requested: The validated new connection details.
        token_changed: Whether the edit replaces the personal access token.

    Returns:
        The details to store.
    """
    carried = {
        key: value
        for key, value in (previous or {}).items()
        if key in DERIVED_KEYS and not (token_changed and key in DERIVED_IDENTITY_KEYS)
    }
    return {**carried, **requested}
