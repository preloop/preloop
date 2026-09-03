"""
Event type normalization for webhook events.

Converts tracker-specific event names (GitHub, GitLab, Jira) to normalized event types
that can be used for flow triggers.

Also extracts filter-relevant fields from webhook payloads to enable
conditional flow triggering based on author, labels, assignee, etc.
"""

from typing import Dict, Any, List, Optional, Tuple

from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY


def gitlab_label_delta(payload: Optional[dict]) -> Tuple[List[str], List[str]]:
    """Return (added, removed) label titles from a GitLab issue/MR webhook.

    GitLab does not emit a dedicated labeled event. Label edits arrive as
    ``Issue Hook`` / ``Merge Request Hook`` with ``action=update`` and a
    ``changes.labels`` previous/current pair.

    A single GitLab edit can both add and remove labels. This helper always
    returns both deltas. Callers that need one event type
    (``normalize_event_type``) give additions precedence: if ``added`` is
    non-empty the edit is ``issue_labeled``, even when ``removed`` is also
    set. ``extract_filter_fields`` still exposes both lists so a labeled
    flow can filter on the removed titles.
    """
    if not payload:
        return [], []
    changes = payload.get("changes") or {}
    labels_change = changes.get("labels") or {}
    if not isinstance(labels_change, dict):
        return [], []

    def _titles(entries: Any) -> set[str]:
        titles: set[str] = set()
        for item in entries or []:
            if isinstance(item, dict):
                title = item.get("title")
                if title:
                    titles.add(str(title))
            elif isinstance(item, str) and item.strip():
                titles.add(item)
        return titles

    previous = _titles(labels_change.get("previous"))
    current = _titles(labels_change.get("current"))
    return sorted(current - previous), sorted(previous - current)


# Mapping of GitLab webhook events to normalized event types
GITLAB_EVENT_MAP: Dict[str, str] = {
    "Issue Hook": "issue_opened",
    "Note Hook": "comment_created",
    "Merge Request Hook": "merge_request_opened",
    "Push Hook": "push",
    "Tag Push Hook": "tag_push",
    "Pipeline Hook": "pipeline",
    "Job Hook": "job",
    "Deployment Hook": "deployment",
    "Release Hook": "release",
}

# Mapping of GitHub webhook events to normalized event types
GITHUB_EVENT_MAP: Dict[str, str] = {
    "issues": "issue_opened",
    "issue_comment": "comment_created",
    "pull_request": "pull_request_opened",
    "push": "push",
    "release": "release",
    "deployment_status": "deployment",
}

# Mapping of Jira webhook events to normalized event types
JIRA_EVENT_MAP: Dict[str, str] = {
    "jira:issue_created": "issue_opened",
    "jira:issue_updated": "issue_updated",
    "jira:issue_deleted": "issue_deleted",
    "comment_created": "comment_created",
    "comment_updated": "comment_updated",
    "comment_deleted": "comment_deleted",
}


# Human-readable labels for normalized event types.
# Mirrors frontend/src/constants/tracker-event-types.ts so the subject rendered
# in the console reads the same as the event picker used to configure the flow.
EVENT_TYPE_LABELS: Dict[str, str] = {
    "issue_opened": "Issue Opened",
    "issue_updated": "Issue Updated",
    "issue_closed": "Issue Closed",
    "issue_reopened": "Issue Reopened",
    "issue_deleted": "Issue Deleted",
    "issue_labeled": "Issue Labeled",
    "issue_unlabeled": "Issue Unlabeled",
    "issue_assigned": "Issue Assigned",
    "issue_unassigned": "Issue Unassigned",
    "pull_request_opened": "Pull Request Opened",
    "pull_request_updated": "Pull Request Updated",
    "pull_request_closed": "Pull Request Closed",
    "pull_request_merged": "Pull Request Merged",
    "pull_request_reopened": "Pull Request Reopened",
    "pull_request_review_requested": "Pull Request Review Requested",
    "pull_request_ready_for_review": "Pull Request Ready for Review",
    "merge_request_opened": "Merge Request Opened",
    "merge_request_updated": "Merge Request Updated",
    "merge_request_closed": "Merge Request Closed",
    "merge_request_merged": "Merge Request Merged",
    "merge_request_approved": "Merge Request Approved",
    "merge_request_reopened": "Merge Request Reopened",
    "comment_created": "Comment Created",
    "comment_updated": "Comment Updated",
    "comment_deleted": "Comment Deleted",
    "push": "Push to Repository",
    "tag_push": "Tag Push",
    "pipeline": "Pipeline Event",
    "release": "Release Published",
    "deployment": "Deployment",
    "job": "Job Event",
    "webhook": "Webhook",
}

# Reserved key under which the compact subject is stored inside
# FlowExecution.trigger_event_details. Leading underscore keeps it clearly
# distinct from raw webhook payload keys. Re-exported from the model so the
# writer here and the reader in the CRUD layer cannot drift apart.
SUBJECT_KEY = TRIGGER_SUBJECT_KEY

# Separator used to join subject parts, matching the console's visual style.
_SUBJECT_SEPARATOR = " · "


def humanize_event_type(event_type: Optional[str]) -> str:
    """Convert a normalized event type into a human-readable label.

    Falls back to title-casing the raw value so unknown or future event types
    still render as something a person can read rather than a bare slug.

    Args:
        event_type: Normalized event type (e.g. 'pull_request_updated').

    Returns:
        Human-readable label (e.g. 'Pull Request Updated'). Empty string if
        no event type was supplied.
    """
    if not event_type:
        return ""
    known = EVENT_TYPE_LABELS.get(event_type)
    if known:
        return known
    return event_type.replace("_", " ").replace("-", " ").title()


def normalize_event_type(
    tracker_type: str, raw_event_type: str, payload: dict = None
) -> str:
    """
    Normalize a tracker-specific event type to a standard event type.

    Args:
        tracker_type: The tracker type (e.g., 'gitlab', 'github', 'jira')
        raw_event_type: The raw event type from the webhook
        payload: Optional webhook payload for additional context

    Returns:
        Normalized event type string
    """
    tracker_type_lower = tracker_type.lower()

    if tracker_type_lower == "gitlab":
        # GitLab events - use the event type from header
        normalized = GITLAB_EVENT_MAP.get(raw_event_type)

        # For GitLab, we can refine based on action in payload
        if normalized == "issue_opened" and payload:
            action = payload.get("object_attributes", {}).get("action")
            if action == "update":
                added, removed = gitlab_label_delta(payload)
                if added:
                    # Added wins: a mixed add+remove edit is issue_labeled,
                    # matching the intake-to-implementation hop. GitHub
                    # emits separate labeled and unlabeled events for the
                    # same edit; GitLab cannot. issue_unlabeled flows do
                    # not fire for mixed edits. Both deltas still land in
                    # filter_fields.
                    normalized = "issue_labeled"
                elif removed:
                    normalized = "issue_unlabeled"
                else:
                    normalized = "issue_updated"
            elif action == "close":
                normalized = "issue_closed"
            elif action == "reopen":
                normalized = "issue_reopened"
        elif normalized == "merge_request_opened" and payload:
            action = payload.get("object_attributes", {}).get("action")
            if action == "update":
                normalized = "merge_request_updated"
            elif action == "close":
                normalized = "merge_request_closed"
            elif action == "reopen":
                normalized = "merge_request_reopened"
            elif action == "merge":
                normalized = "merge_request_merged"
            elif action == "approved":
                normalized = "merge_request_approved"

        return normalized or raw_event_type

    elif tracker_type_lower == "github":
        # GitHub events - use the event type from header
        normalized = GITHUB_EVENT_MAP.get(raw_event_type)

        # For GitHub, we can refine based on action in payload
        if normalized and payload:
            action = payload.get("action")
            if action:
                # Map specific actions
                if normalized == "issue_opened":
                    if action == "opened":
                        pass  # Keep as issue_opened
                    elif action == "edited":
                        normalized = "issue_updated"
                    elif action == "closed":
                        normalized = "issue_closed"
                    elif action == "reopened":
                        normalized = "issue_reopened"
                    elif action == "labeled":
                        normalized = "issue_labeled"
                    elif action == "unlabeled":
                        normalized = "issue_unlabeled"
                    elif action == "assigned":
                        normalized = "issue_assigned"
                    elif action == "unassigned":
                        normalized = "issue_unassigned"
                elif normalized == "pull_request_opened":
                    if action == "opened":
                        pass  # Keep as pull_request_opened
                    elif action == "edited":
                        # Title, description, or base branch changed
                        normalized = "pull_request_updated"
                    elif action == "synchronize":
                        # New commits pushed to the PR - also treated as "updated"
                        # This matches user expectation that "PR Updated" includes new commits
                        normalized = "pull_request_updated"
                    elif action == "closed":
                        if payload.get("pull_request", {}).get("merged"):
                            normalized = "pull_request_merged"
                        else:
                            normalized = "pull_request_closed"
                    elif action == "reopened":
                        normalized = "pull_request_reopened"
                    elif action == "review_requested":
                        normalized = "pull_request_review_requested"
                    elif action == "ready_for_review":
                        normalized = "pull_request_ready_for_review"
                elif normalized == "comment_created":
                    if action == "created":
                        pass  # Keep as comment_created
                    elif action == "edited":
                        normalized = "comment_updated"
                    elif action == "deleted":
                        normalized = "comment_deleted"

        return normalized or raw_event_type

    elif tracker_type_lower == "jira":
        # Jira events - already normalized in webhook
        return JIRA_EVENT_MAP.get(raw_event_type, raw_event_type)

    # Unknown tracker type - return as-is
    return raw_event_type


def _short_sha(sha: Optional[str]) -> Optional[str]:
    """Shorten a commit SHA to the conventional 8-character prefix."""
    if not sha or not isinstance(sha, str):
        return None
    return sha[:8]


def _branch_from_ref(ref: Optional[str]) -> Optional[str]:
    """Turn a git ref like 'refs/heads/main' into a short branch name."""
    if not ref or not isinstance(ref, str):
        return None
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _github_subject(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract subject parts from a GitHub webhook payload."""
    parts: Dict[str, Any] = {}

    repo = payload.get("repository") or {}
    if isinstance(repo, dict) and repo.get("full_name"):
        parts["repo"] = repo["full_name"]

    pr = payload.get("pull_request")
    if isinstance(pr, dict) and pr:
        if pr.get("number"):
            parts["reference"] = f"#{pr['number']}"
        if pr.get("title"):
            parts["title"] = pr["title"]
        if pr.get("html_url"):
            parts["url"] = pr["html_url"]
        head = pr.get("head")
        if isinstance(head, dict):
            parts["commit"] = _short_sha(head.get("sha"))
        return parts

    issue = payload.get("issue")
    if isinstance(issue, dict) and issue:
        if issue.get("number"):
            parts["reference"] = f"#{issue['number']}"
        if issue.get("title"):
            parts["title"] = issue["title"]
        if issue.get("html_url"):
            parts["url"] = issue["html_url"]
        return parts

    # Push event
    if payload.get("head_commit") or payload.get("after"):
        branch = _branch_from_ref(payload.get("ref"))
        if branch:
            parts["reference"] = branch
        head_commit = payload.get("head_commit")
        sha = None
        if isinstance(head_commit, dict):
            sha = head_commit.get("id")
        parts["commit"] = _short_sha(sha or payload.get("after"))
        if payload.get("compare"):
            parts["url"] = payload["compare"]
        return parts

    # Release event
    release = payload.get("release")
    if isinstance(release, dict) and release:
        if release.get("tag_name"):
            parts["reference"] = release["tag_name"]
        if release.get("html_url"):
            parts["url"] = release["html_url"]

    return parts


def _gitlab_subject(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract subject parts from a GitLab webhook payload."""
    parts: Dict[str, Any] = {}

    project = payload.get("project") or {}
    if isinstance(project, dict) and project.get("path_with_namespace"):
        parts["repo"] = project["path_with_namespace"]

    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict) and obj_attrs:
        object_kind = payload.get("object_kind") or ""
        iid = obj_attrs.get("iid")
        if iid:
            # GitLab renders merge requests as !N and issues as #N.
            prefix = "!" if object_kind == "merge_request" else "#"
            parts["reference"] = f"{prefix}{iid}"
        if obj_attrs.get("title"):
            parts["title"] = obj_attrs["title"]
        if obj_attrs.get("url"):
            parts["url"] = obj_attrs["url"]

        last_commit = obj_attrs.get("last_commit")
        sha = None
        if isinstance(last_commit, dict):
            sha = last_commit.get("id")
        parts["commit"] = _short_sha(sha or obj_attrs.get("sha"))
        return parts

    # Push / tag push event
    if payload.get("after") or payload.get("checkout_sha"):
        branch = _branch_from_ref(payload.get("ref"))
        if branch:
            parts["reference"] = branch
        parts["commit"] = _short_sha(
            payload.get("checkout_sha") or payload.get("after")
        )

    return parts


def _jira_subject(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract subject parts from a Jira webhook payload."""
    parts: Dict[str, Any] = {}

    issue = payload.get("issue")
    if isinstance(issue, dict) and issue:
        if issue.get("key"):
            parts["reference"] = issue["key"]
        fields = issue.get("fields")
        if isinstance(fields, dict):
            project = fields.get("project")
            if isinstance(project, dict) and project.get("key"):
                parts["repo"] = project["key"]
            if fields.get("summary"):
                parts["title"] = fields["summary"]

    return parts


def extract_trigger_subject(event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a compact, human-readable subject for a flow execution.

    The subject makes an execution identifiable at a glance in the console
    list without having to open it and read the raw trigger payload. It is
    computed once, when the execution is created, and stored denormalized on
    the execution row so list queries stay cheap.

    For a tracker-triggered review the subject reads like:
        'preloop/preloop #78 · Pull Request Updated · 5167595c'

    For trigger types that carry less identity the subject degrades
    gracefully, falling back to the event label alone.

    Args:
        event_data: The standardized event envelope, with 'source', 'type'
            and 'payload' keys, as stored in
            FlowExecution.trigger_event_details.

    Returns:
        A dict with a rendered 'text' key plus whichever of 'repo',
        'reference', 'title', 'event', 'commit' and 'url' could be
        determined, or None if nothing identifying could be extracted.
    """
    if not isinstance(event_data, dict):
        return None

    source = str(event_data.get("source") or "").lower()
    event_type = event_data.get("type")
    payload = event_data.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if source == "github":
        parts = _github_subject(payload)
    elif source == "gitlab":
        parts = _gitlab_subject(payload)
    elif source == "jira":
        parts = _jira_subject(payload)
    else:
        parts = {}

    # Drop keys that resolved to None so the stored blob stays compact.
    parts = {key: value for key, value in parts.items() if value}

    label = humanize_event_type(event_type)
    if event_data.get("test_mode"):
        label = "Manual Test Run"
    if label:
        parts["event"] = label

    # Render the display string: repo · reference · event · commit.
    # Repo and reference are joined by a space because they read as one
    # identifier ('preloop/preloop #78').
    identifier = " ".join(
        value for value in (parts.get("repo"), parts.get("reference")) if value
    )
    segments = [
        segment
        for segment in (identifier, parts.get("event"), parts.get("commit"))
        if segment
    ]
    if not segments:
        return None

    parts["text"] = _SUBJECT_SEPARATOR.join(segments)
    return parts


def attach_trigger_subject(
    trigger_details: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Compute the execution subject and store it on the trigger snapshot.

    Called at execution-creation time so the console list never has to parse
    the full trigger payload to label a row. Mutates and returns the same
    dict for convenience; a snapshot with no extractable subject is left
    untouched.

    Args:
        trigger_details: The trigger event snapshot about to be persisted.

    Returns:
        The same object, with the subject stored under SUBJECT_KEY when one
        could be derived.
    """
    if not isinstance(trigger_details, dict):
        return trigger_details
    subject = extract_trigger_subject(trigger_details)
    if subject:
        trigger_details[SUBJECT_KEY] = subject
    return trigger_details


def extract_filter_fields(
    tracker_type: str, raw_event_type: str, payload: dict
) -> Dict[str, Any]:
    """
    Extract filter-relevant fields from webhook payload.

    Returns a dictionary with standardized fields that can be used in trigger_config:
    - author: Username of the person who created the issue/PR
    - assignee: Username of the assigned person (or list of usernames)
    - reviewer: Username of requested reviewer (or list of usernames) for PRs/MRs
    - labels: List of label names
    - milestone: Milestone name
    - priority: Priority level (for Jira)
    - state: Current state/status
    - action: Specific action that triggered the event

    Args:
        tracker_type: The tracker type (e.g., 'gitlab', 'github', 'jira')
        raw_event_type: The raw event type from the webhook
        payload: The webhook payload

    Returns:
        Dictionary with extracted filter fields
    """
    tracker_type_lower = tracker_type.lower()
    filter_fields: Dict[str, Any] = {}

    if tracker_type_lower == "gitlab":
        # Extract from object_attributes for issues/MRs
        obj_attrs = payload.get("object_attributes", {})
        user = payload.get("user", {})

        # Author
        filter_fields["author"] = user.get("username")

        # Assignees (can be multiple in GitLab)
        assignees = payload.get("assignees", [])
        if assignees:
            filter_fields["assignee"] = [a.get("username") for a in assignees]
        elif obj_attrs.get("assignee_id"):
            # Single assignee fallback
            assignee = payload.get("assignee")
            if assignee:
                filter_fields["assignee"] = assignee.get("username")

        # Labels
        labels = payload.get("labels", [])
        if labels:
            filter_fields["labels"] = [label.get("title") for label in labels]

        added_labels, removed_labels = gitlab_label_delta(payload)
        if added_labels:
            filter_fields["added_labels"] = added_labels
        if removed_labels:
            filter_fields["removed_labels"] = removed_labels

        # Milestone
        milestone = obj_attrs.get("milestone")
        if milestone:
            filter_fields["milestone"] = milestone.get("title")

        # State and action
        filter_fields["state"] = obj_attrs.get("state")
        filter_fields["action"] = obj_attrs.get("action")

        # Merge Request specific fields
        if "merge_request" in payload.get("object_kind", ""):
            # Reviewers (for merge requests)
            reviewers = payload.get("reviewers", [])
            if reviewers:
                filter_fields["reviewer"] = [r.get("username") for r in reviewers]

            # Check if MR is merged
            filter_fields["merged"] = (
                obj_attrs.get("merge_status") == "merged"
                or obj_attrs.get("state") == "merged"
            )

            # Draft status (work_in_progress)
            filter_fields["draft"] = obj_attrs.get(
                "work_in_progress", False
            ) or obj_attrs.get("draft", False)

            # Merge status (can_merge, cannot_merge, etc.)
            filter_fields["merge_status"] = obj_attrs.get("merge_status")

            # Detailed state includes info about approval
            filter_fields["detailed_merge_status"] = obj_attrs.get(
                "detailed_merge_status"
            )

        if payload.get("object_kind") == "build" or payload.get("build_name"):
            if payload.get("build_name"):
                filter_fields["build_name"] = payload.get("build_name")
            if payload.get("build_status"):
                filter_fields["build_status"] = payload.get("build_status")
            if payload.get("build_stage"):
                filter_fields["build_stage"] = payload.get("build_stage")
            if payload.get("ref"):
                filter_fields["ref"] = payload.get("ref")

    elif tracker_type_lower == "github":
        # GitHub structure varies by event type
        action = payload.get("action")
        filter_fields["action"] = action

        label_obj = payload.get("label")
        label_name = None
        if isinstance(label_obj, dict):
            label_name = label_obj.get("name")
        elif isinstance(label_obj, str):
            label_name = label_obj
        if label_name:
            if action == "labeled":
                filter_fields["added_labels"] = [label_name]
            elif action == "unlabeled":
                filter_fields["removed_labels"] = [label_name]

        # Extract from issue object
        if "issue" in payload:
            issue = payload["issue"]

            # Author
            user = issue.get("user", {})
            filter_fields["author"] = user.get("login")

            # Assignees (can be multiple in GitHub)
            assignees = issue.get("assignees", [])
            if assignees:
                filter_fields["assignee"] = [a.get("login") for a in assignees]

            # Labels
            labels = issue.get("labels", [])
            if labels:
                filter_fields["labels"] = [label.get("name") for label in labels]

            # Milestone
            milestone = issue.get("milestone")
            if milestone:
                filter_fields["milestone"] = milestone.get("title")

            # State
            filter_fields["state"] = issue.get("state")
            if issue.get("state_reason"):
                filter_fields["state_reason"] = issue.get("state_reason")

        # Extract from pull_request object
        elif "pull_request" in payload:
            pr = payload["pull_request"]

            # Author
            user = pr.get("user", {})
            filter_fields["author"] = user.get("login")

            # Assignees
            assignees = pr.get("assignees", [])
            if assignees:
                filter_fields["assignee"] = [a.get("login") for a in assignees]

            # Reviewers (requested reviewers for pull requests)
            requested_reviewers = pr.get("requested_reviewers", [])
            if requested_reviewers:
                filter_fields["reviewer"] = [
                    r.get("login") for r in requested_reviewers
                ]

            # For review_requested action, also check top-level requested_reviewer
            # This is the specific reviewer that was just added (not in the PR object)
            if action == "review_requested":
                top_level_reviewer = payload.get("requested_reviewer", {})
                if top_level_reviewer:
                    reviewer_login = top_level_reviewer.get("login")
                    if reviewer_login:
                        # Ensure reviewer list includes the just-added reviewer
                        if "reviewer" not in filter_fields:
                            filter_fields["reviewer"] = []
                        if reviewer_login not in filter_fields["reviewer"]:
                            filter_fields["reviewer"].append(reviewer_login)

            # Labels
            labels = pr.get("labels", [])
            if labels:
                filter_fields["labels"] = [label.get("name") for label in labels]

            # Milestone
            milestone = pr.get("milestone")
            if milestone:
                filter_fields["milestone"] = milestone.get("title")

            # State
            filter_fields["state"] = pr.get("state")

            # Pull Request specific fields
            # Merged status
            filter_fields["merged"] = pr.get("merged", False)

            # Draft status
            filter_fields["draft"] = pr.get("draft", False)

            # Mergeable status (can be merged)
            filter_fields["mergeable"] = pr.get("mergeable")

            # Merge state status (clean, dirty, unstable, blocked, etc.)
            filter_fields["mergeable_state"] = pr.get("mergeable_state")

        # Sender (who triggered the event)
        sender = payload.get("sender", {})
        filter_fields["sender"] = sender.get("login")

        deployment = payload.get("deployment") or {}
        deployment_status = payload.get("deployment_status") or {}
        if deployment or deployment_status:
            environment = deployment_status.get("environment") or deployment.get(
                "environment"
            )
            if environment:
                filter_fields["environment"] = environment
            dep_state = deployment_status.get("state")
            if dep_state:
                filter_fields["state"] = dep_state

    elif tracker_type_lower == "jira":
        # Jira webhook structure
        issue = payload.get("issue", {})
        fields = issue.get("fields", {})
        user = payload.get("user", {})

        # Author/Creator
        creator = fields.get("creator", {})
        filter_fields["author"] = creator.get("displayName") or creator.get("accountId")

        # Reporter
        reporter = fields.get("reporter", {})
        filter_fields["reporter"] = reporter.get("displayName") or reporter.get(
            "accountId"
        )

        # Assignee
        assignee = fields.get("assignee")
        if assignee:
            filter_fields["assignee"] = assignee.get("displayName") or assignee.get(
                "accountId"
            )

        # Labels
        labels = fields.get("labels", [])
        if labels:
            filter_fields["labels"] = labels

        # Priority
        priority = fields.get("priority")
        if priority:
            filter_fields["priority"] = priority.get("name")

        # Status
        status = fields.get("status")
        if status:
            filter_fields["state"] = status.get("name")

        # Issue type
        issue_type = fields.get("issuetype")
        if issue_type:
            filter_fields["issue_type"] = issue_type.get("name")

        # User who triggered the event
        filter_fields["event_user"] = user.get("displayName") or user.get("accountId")

    return filter_fields
