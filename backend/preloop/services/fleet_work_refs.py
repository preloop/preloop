"""Classify governed tool calls and shell commands into work-item references.

The Fleet Map links agents and flows to the issues and pull/merge requests
they touched. This module turns one tool call into a compact, bounded
``work_ref`` dictionary that the call recorders persist in the activity row's
metadata:

* built-in Preloop MCP tools (``create_issue``, ``update_pull_request``, ...),
* the GitHub and GitLab MCP servers, by server-name pattern and tool name
  (untested against live servers; tool names follow github/github-mcp-server
  and gitlab-mcp),
* ``gh`` and ``glab`` commands run through a native ``Bash`` tool, seen by the
  PreToolUse permission hook.

Privacy rail: only the classified verb, the object reference
(``owner/repo#N`` or ``!N``), the repository, the forge host and references
parsed from a title/body are kept. Never the arguments, the body or the
command line.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Literal
from urllib.parse import urlsplit

Via = Literal["builtin", "cli", "github_mcp", "gitlab_mcp"]
Kind = Literal["issue", "pull_request"]

MAX_REFS = 5
_REPO = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")
_OBJECT_PATH = re.compile(
    r"^/(?P<repo>.+?)/(?:-/)?(?P<type>issues|pull|pulls|merge_requests|pull-requests)/(?P<n>[0-9]+)(?:/.*)?$"
)
_SHORT = re.compile(r"^(?P<repo>[\w.-]+(?:/[\w.-]+)+)?(?P<mark>[#!])(?P<n>[0-9]+)$")
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"(?P<ref>(?:https://\S+)|(?:[\w.-]+/[\w.-]+)?[#!][0-9]+)",
    re.IGNORECASE,
)
_TRACKER_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
_MENTION = re.compile(r"(?:https://\S+|(?<![\w/])(?:[\w.-]+/[\w.-]+)?#[0-9]+)")


def _bounded(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _repo(value: Any) -> str | None:
    """``owner/repo`` (any depth for GitLab groups), lowercased, or None."""
    text = _bounded(value, 300)
    if text is None:
        return None
    if text.startswith("https://"):
        path = urlsplit(text).path.strip("/")
        text = path.split("/-/")[0]
    text = text.strip("/").removesuffix(".git")
    return text.lower() if _REPO.fullmatch(text) else None


def parse_reference(
    value: Any, *, repo: str | None = None, kind: Kind | None = None
) -> dict[str, Any] | None:
    """Parse one explicit object reference.

    Args:
        value: A forge URL, ``owner/repo#N``, ``#N``, ``!N`` or a bare number.
        repo: Repository used to qualify a short reference.
        kind: Expected kind; a bare number needs it.

    Returns:
        ``{"kind", "ref", "repo", "host"}`` or None when nothing explicit is
        present. ``kind`` is None for an ambiguous GitHub ``#N``.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        value = str(value)
    text = _bounded(value, 1000)
    if text is None:
        return None
    if text.startswith("https://"):
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        match = _OBJECT_PATH.match(parts.path or "")
        if not match or not parts.hostname:
            return None
        found: Kind = "issue" if match["type"] == "issues" else "pull_request"
        mark = "!" if match["type"] == "merge_requests" else "#"
        return {
            "kind": found,
            "ref": f"{mark}{int(match['n'])}",
            "repo": _repo(match["repo"]),
            "host": parts.hostname.lower(),
        }
    if text.isdigit():
        if kind is None:
            return None
        mark = "#"
        text = f"{mark}{text}"
    match = _SHORT.fullmatch(text)
    if not match:
        return None
    found_kind: Kind | None = kind
    if match["mark"] == "!":
        found_kind = "pull_request"
    return {
        "kind": found_kind,
        "ref": f"{match['mark']}{int(match['n'])}",
        "repo": _repo(match["repo"]) if match["repo"] else repo,
        "host": None,
    }


def _key(ref: dict[str, Any]) -> str | None:
    return f"{ref['repo']}{ref['ref']}" if ref.get("repo") else None


def text_references(text: Any, *, repo: str | None) -> tuple[list[str], list[str]]:
    """Closing and plain issue references named in a title or body.

    Only the parsed keys are returned, never the text.
    """
    body = text if isinstance(text, str) else ""
    body = body[:20000]
    closes: list[str] = []
    for match in _CLOSING.finditer(body):
        ref = parse_reference(match["ref"], repo=repo, kind="issue")
        if ref and ref["ref"].startswith("#") and (key := _key(ref)):
            if key not in closes:
                closes.append(key)
    mentions: list[str] = []
    for match in _MENTION.finditer(body):
        ref = parse_reference(match.group(0).rstrip(").,;:"), repo=repo, kind="issue")
        if (
            ref
            and ref["kind"] in {None, "issue"}
            and ref["ref"].startswith("#")
            and (key := _key(ref))
            and key not in closes
            and key not in mentions
        ):
            mentions.append(key)
    return closes[:MAX_REFS], mentions[:MAX_REFS]


def _result(
    via: Via,
    kind: Kind | None,
    action: str,
    ref: dict[str, Any] | None = None,
    *,
    repo: str | None = None,
    review_state: str | None = None,
    text: Any = None,
) -> dict[str, Any]:
    repository = (ref or {}).get("repo") or repo
    out: dict[str, Any] = {
        "via": via,
        "kind": (ref or {}).get("kind") or kind,
        "action": action,
        "ref": (ref or {}).get("ref"),
        "repo": repository,
    }
    if (ref or {}).get("host"):
        out["host"] = ref["host"]  # type: ignore[index]
    if review_state:
        out["review_state"] = review_state
    if text is not None:
        closes, mentions = text_references(text, repo=repository)
        if closes:
            out["closes"] = closes
        if mentions:
            out["mentions"] = mentions
    return out


_REVIEW_STATES = {
    "approve": "approved",
    "approved": "approved",
    "request_changes": "changes_requested",
    "changes_requested": "changes_requested",
    "comment": "commented",
    "commented": "commented",
}


def _builtin(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    project = _repo(args.get("project"))
    if tool == "create_issue":
        return _result(
            "builtin",
            "issue",
            "issue_opened",
            repo=project,
            text=f"{args.get('title') or ''}\n{args.get('description') or ''}",
        )
    if tool == "update_issue":
        ref = parse_reference(args.get("issue"), kind="issue")
        found = _result("builtin", "issue", "issue_updated", ref)
        key = _bounded(args.get("issue"), 64)
        if ref is None and key and _TRACKER_KEY.fullmatch(key.upper()):
            # A tracker key (``PROJ-123``) is a complete identity on its own.
            found["key"] = key.upper()
        return found
    if tool in {"add_comment", "update_comment"}:
        ref = parse_reference(args.get("target"))
        if ref is not None and ref["kind"] is None:
            # The comment router reads an ``owner/repo#N`` target as a PR/MR.
            ref["kind"] = "pull_request"
        if args.get("path") and args.get("line") is not None:
            ref = dict(ref or {}, kind="pull_request")
        return _result(
            "builtin",
            (ref or {}).get("kind"),
            "commented",
            ref,
            text=args.get("comment") or args.get("body"),
        )
    if tool == "create_pull_request":
        return _result(
            "builtin",
            "pull_request",
            "opened",
            repo=project,
            text=f"{args.get('title') or ''}\n{args.get('description') or ''}",
        )
    if tool == "update_pull_request":
        ref = parse_reference(args.get("pull_request"), kind="pull_request")
        if ref:
            ref["kind"] = "pull_request"
        review = _bounded(args.get("review_action"), 40)
        if review:
            return _result(
                "builtin",
                "pull_request",
                "reviewed",
                ref,
                review_state=_REVIEW_STATES.get(review.lower()),
            )
        state = (_bounded(args.get("state"), 20) or "").lower()
        action = "merged" if state == "merged" else "pull_request_updated"
        return _result(
            "builtin", "pull_request", action, ref, text=args.get("description")
        )
    if tool in {"merge_pull_request", "merge_merge_request"}:
        ref = parse_reference(args.get("pull_request"), kind="pull_request")
        return _result("builtin", "pull_request", "merged", ref)
    return None


def _number(args: dict[str, Any], *names: str) -> Any:
    for name in names:
        if args.get(name) not in (None, ""):
            return args.get(name)
    return None


# GitHub MCP server (github/github-mcp-server). Untested live.
_GITHUB: dict[str, tuple[Kind, str]] = {
    "create_issue": ("issue", "issue_opened"),
    "update_issue": ("issue", "issue_updated"),
    "add_issue_comment": ("issue", "commented"),
    "create_pull_request": ("pull_request", "opened"),
    "update_pull_request": ("pull_request", "pull_request_updated"),
    "merge_pull_request": ("pull_request", "merged"),
    "create_pull_request_review": ("pull_request", "reviewed"),
    "pull_request_review_write": ("pull_request", "reviewed"),
    "submit_pending_pull_request_review": ("pull_request", "reviewed"),
    "add_comment_to_pending_review": ("pull_request", "commented"),
    "add_pull_request_review_comment": ("pull_request", "commented"),
    "issue_write": ("issue", "issue_updated"),
}
# GitLab MCP (gitlab-mcp). Untested live.
_GITLAB: dict[str, tuple[Kind, str]] = {
    "create_issue": ("issue", "issue_opened"),
    "update_issue": ("issue", "issue_updated"),
    "create_issue_note": ("issue", "commented"),
    "create_merge_request": ("pull_request", "opened"),
    "update_merge_request": ("pull_request", "pull_request_updated"),
    "merge_merge_request": ("pull_request", "merged"),
    "approve_merge_request": ("pull_request", "reviewed"),
    "create_merge_request_note": ("pull_request", "commented"),
    "create_merge_request_thread": ("pull_request", "commented"),
    "create_note": ("issue", "commented"),
}


def _table_entry(
    table: dict[str, tuple[Kind, str]], tool: str
) -> tuple[str, tuple[Kind, str]] | None:
    """Match the upstream name, allowing a ``<prefix>_`` collision prefix."""
    if tool in table:
        return tool, table[tool]
    for name in sorted(table, key=len, reverse=True):
        if tool.endswith(f"_{name}"):
            return name, table[name]
    return None


def _github(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    entry = _table_entry(_GITHUB, tool)
    if entry is None:
        return None
    name, (kind, action) = entry
    owner, repo_name = _bounded(args.get("owner"), 100), _bounded(args.get("repo"), 100)
    repo = _repo(f"{owner}/{repo_name}") if owner and repo_name else None
    if name == "issue_write" and (args.get("method") == "create"):
        action = "issue_opened"
    number = _number(args, "issue_number", "pullNumber", "pull_number", "number")
    ref = parse_reference(number, repo=repo, kind=kind) if number else None
    if ref:
        ref["kind"] = kind
    review_state = None
    if action == "reviewed":
        event = (_bounded(args.get("event"), 40) or "").lower()
        review_state = _REVIEW_STATES.get(event)
    if action == "issue_updated" and (args.get("state") == "closed"):
        action = "issue_updated"
    return _result(
        "github_mcp",
        kind,
        action,
        ref,
        repo=repo,
        review_state=review_state,
        text=f"{args.get('title') or ''}\n{args.get('body') or ''}"
        if action in {"opened", "issue_opened"}
        else None,
    )


def _gitlab(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    entry = _table_entry(_GITLAB, tool)
    if entry is None:
        return None
    name, (kind, action) = entry
    repo = (
        _repo(args.get("project_id"))
        if isinstance(args.get("project_id"), str)
        else None
    )
    if name == "create_note":
        kind = (
            "pull_request"
            if str(args.get("noteable_type", "")).lower()
            in {"merge_request", "mergerequest"}
            else "issue"
        )
    number = _number(args, "merge_request_iid", "issue_iid", "noteable_iid", "iid")
    ref = None
    if number is not None and str(number).isdigit():
        mark = "!" if kind == "pull_request" else "#"
        ref = {"kind": kind, "ref": f"{mark}{int(number)}", "repo": repo, "host": None}
    return _result(
        "gitlab_mcp",
        kind,
        action,
        ref,
        repo=repo,
        review_state="approved" if name == "approve_merge_request" else None,
        text=f"{args.get('title') or ''}\n{args.get('description') or ''}"
        if action in {"opened", "issue_opened"}
        else None,
    )


def classify_tool_call(
    server_name: str | None, tool_name: str | None, arguments: Any
) -> dict[str, Any] | None:
    """Classify one MCP tool call; reads (``get_*``, search) return None.

    Args:
        server_name: ``preloop-mcp`` for built-ins, else the proxied server name.
        tool_name: Client-visible tool name.
        arguments: Call arguments (read, never stored).

    Returns:
        The compact ``work_ref`` dictionary or None.
    """
    tool = (tool_name or "").strip()
    if not tool or not isinstance(arguments, dict):
        return None
    server = (server_name or "").lower()
    try:
        if server in {"", "preloop-mcp", "preloop"}:
            return _builtin(tool, arguments)
        if "github" in server:
            return _github(tool, arguments)
        if "gitlab" in server:
            return _gitlab(tool, arguments)
    except Exception:  # noqa: BLE001 - classification must never fail a call
        return None
    return None


# ----------------------------------------------------------------------------
# gh / glab commands


_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_GH_VERBS: dict[tuple[str, str], tuple[Kind, str]] = {
    ("pr", "create"): ("pull_request", "opened"),
    ("pr", "comment"): ("pull_request", "commented"),
    ("pr", "review"): ("pull_request", "reviewed"),
    ("pr", "merge"): ("pull_request", "merged"),
    ("pr", "close"): ("pull_request", "pull_request_updated"),
    ("pr", "reopen"): ("pull_request", "pull_request_updated"),
    ("pr", "edit"): ("pull_request", "pull_request_updated"),
    ("pr", "ready"): ("pull_request", "pull_request_updated"),
    ("issue", "create"): ("issue", "issue_opened"),
    ("issue", "comment"): ("issue", "commented"),
    ("issue", "close"): ("issue", "issue_updated"),
    ("issue", "reopen"): ("issue", "issue_updated"),
    ("issue", "edit"): ("issue", "issue_updated"),
}
_GLAB_VERBS: dict[tuple[str, str], tuple[Kind, str]] = {
    ("mr", "create"): ("pull_request", "opened"),
    ("mr", "note"): ("pull_request", "commented"),
    ("mr", "comment"): ("pull_request", "commented"),
    ("mr", "approve"): ("pull_request", "reviewed"),
    ("mr", "merge"): ("pull_request", "merged"),
    ("mr", "close"): ("pull_request", "pull_request_updated"),
    ("mr", "reopen"): ("pull_request", "pull_request_updated"),
    ("mr", "update"): ("pull_request", "pull_request_updated"),
    ("issue", "create"): ("issue", "issue_opened"),
    ("issue", "note"): ("issue", "commented"),
    ("issue", "comment"): ("issue", "commented"),
    ("issue", "close"): ("issue", "issue_updated"),
    ("issue", "reopen"): ("issue", "issue_updated"),
    ("issue", "update"): ("issue", "issue_updated"),
}
_ALIASES = {"new": "create", "mr": "mr", "issues": "issue", "prs": "pr"}
_VALUE_FLAGS = {
    "-R",
    "--repo",
    "-t",
    "--title",
    "-b",
    "--body",
    "-m",
    "--message",
    "-d",
    "--description",
    "-B",
    "--base",
    "-H",
    "--head",
    "-F",
    "--body-file",
    "-l",
    "--label",
    "-a",
    "--assignee",
    "--reviewer",
    "--source-branch",
    "--target-branch",
    "-X",
    "--method",
    "-f",
    "--field",
    "--raw-field",
    "-q",
    "--jq",
    "--header",
    "--milestone",
    "-p",
    "--project",
    "--input",
}
_GH_API = re.compile(
    r"^/?repos/(?P<repo>[\w.-]+/[\w.-]+)/(?P<type>issues|pulls)(?:/(?P<n>[0-9]+))?(?:/(?P<sub>comments|reviews|merge))?/?$"
)


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for part in _SEPARATORS.split(command[:8000]):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if words and words[0].rsplit("/", 1)[-1] in {"gh", "glab"}:
            out.append(words)
    return out


def _flags(words: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    positional: list[str] = []
    values: dict[str, str] = {}
    switches: set[str] = set()
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("-") and len(word) > 1:
            name, eq, inline = word.partition("=")
            if eq:
                values[name] = inline
            elif name in _VALUE_FLAGS and index + 1 < len(words):
                values[name] = words[index + 1]
                index += 1
            else:
                switches.add(name)
        else:
            positional.append(word)
        index += 1
    return positional, values, switches


def _remote_repo(remote: Any) -> tuple[str | None, str | None]:
    """``host/owner/repo`` from the hook's repository identity."""
    text = _bounded(remote, 512)
    if not text:
        return None, None
    host, _, path = text.partition("/")
    return _repo(path), (host.lower() or None)


def classify_shell_command(
    command: Any, *, remote: Any = None
) -> dict[str, Any] | None:
    """Classify a ``gh``/``glab`` write command in a native shell tool call.

    ``view``, ``list``, ``status``, ``diff``, ``checks`` and read-only
    ``gh api`` calls return None. The first classified segment wins.

    Args:
        command: The shell command (read, never stored).
        remote: The hook's ``host/owner/repo`` repository identity, used when
            the command names no repository.

    Returns:
        The compact ``work_ref`` dictionary or None.
    """
    if not isinstance(command, str) or "gh" not in command and "glab" not in command:
        return None
    default_repo, default_host = _remote_repo(remote)
    for words in _segments(command):
        try:
            found = _classify_cli(words, default_repo, default_host)
        except Exception:  # noqa: BLE001 - classification must never fail a call
            found = None
        if found:
            return found
    return None


def _classify_cli(
    words: list[str], default_repo: str | None, default_host: str | None
) -> dict[str, Any] | None:
    tool = words[0].rsplit("/", 1)[-1]
    raw = set(words)
    if tool == "gh" and words[1:3] == ["pr", "review"]:
        # -a/-r/-c are review switches here, not value flags.
        words = [w for w in words if w not in {"-a", "-r", "-c"}]
    positional, values, switches = _flags(words[1:])
    repo = _repo(values.get("-R") or values.get("--repo")) or default_repo
    # The work tree's forge host only applies to the work tree's repository.
    default_host = default_host if repo == default_repo else None
    if tool == "gh" and positional[:1] == ["api"]:
        return _gh_api(positional[1:], values, switches, repo)
    if len(positional) < 2:
        return None
    group = _ALIASES.get(positional[0], positional[0])
    verb = _ALIASES.get(positional[1], positional[1])
    table = _GH_VERBS if tool == "gh" else _GLAB_VERBS
    entry = table.get((group, verb))
    if entry is None:
        return None
    kind, action = entry
    via: Via = "cli"
    target = positional[2] if len(positional) > 2 else None
    ref = parse_reference(target, repo=repo, kind=kind) if target else None
    if ref:
        ref["kind"] = kind
        if tool == "glab" and kind == "pull_request" and ref["ref"].startswith("#"):
            ref["ref"] = "!" + ref["ref"][1:]
        if ref.get("host") is None and ref.get("repo") == repo and default_host:
            ref["host"] = default_host
    review_state = None
    if action == "reviewed":
        if tool == "glab" or raw & {"--approve", "-a"}:
            review_state = "approved"
        elif raw & {"--request-changes", "-r"}:
            review_state = "changes_requested"
        elif raw & {"--comment", "-c"}:
            review_state = "commented"

    text = None
    if action in {"opened", "issue_opened"}:
        text = "\n".join(
            values.get(name, "")
            for name in ("-t", "--title", "-b", "--body", "-d", "--description")
        )
    out = _result(
        via, kind, action, ref, repo=repo, review_state=review_state, text=text
    )
    if out.get("host") is None and default_host:
        out["host"] = default_host
    out["tool"] = tool
    return out


def _gh_api(
    positional: list[str],
    values: dict[str, str],
    switches: set[str],
    repo: str | None,
) -> dict[str, Any] | None:
    if not positional:
        return None
    match = _GH_API.match(positional[0])
    if not match:
        return None
    method = (values.get("-X") or values.get("--method") or "").upper()
    if not method:
        has_fields = (
            any(name in values for name in ("-f", "--field", "-F", "--raw-field"))
            or "--input" in values
        )
        method = "POST" if has_fields else "GET"
    if method == "GET":
        return None
    kind: Kind = "issue" if match["type"] == "issues" else "pull_request"
    number, sub = match["n"], match["sub"]
    if number is None:
        if method != "POST":
            return None
        action = "issue_opened" if kind == "issue" else "opened"
    elif sub == "comments":
        action = "commented"
    elif sub == "reviews":
        action = "reviewed"
    elif sub == "merge":
        action = "merged"
    else:
        action = "issue_updated" if kind == "issue" else "pull_request_updated"
    api_repo = _repo(match["repo"]) or repo
    ref = (
        {"kind": kind, "ref": f"#{int(number)}", "repo": api_repo, "host": None}
        if number
        else None
    )
    out = _result("cli", kind, action, ref, repo=api_repo)
    out["host"] = "github.com"
    out["tool"] = "gh"
    return out


def work_key(work_ref: Any) -> tuple[Kind, str] | None:
    """``(kind, owner/repo#N)`` of a stored ``work_ref``, when fully known."""
    if not isinstance(work_ref, dict):
        return None
    key = work_ref.get("key")
    if (
        work_ref.get("kind") == "issue"
        and isinstance(key, str)
        and _TRACKER_KEY.fullmatch(key)
    ):
        return "issue", key
    kind, ref, repo = work_ref.get("kind"), work_ref.get("ref"), work_ref.get("repo")
    if kind not in {"issue", "pull_request"} or not isinstance(ref, str):
        return None
    if not isinstance(repo, str) or not repo:
        return None
    return kind, f"{repo}{ref}"


def work_url(work_ref: Any) -> str | None:
    """Forge URL of a stored ``work_ref`` when its host is known."""
    found = work_key(work_ref)
    host = work_ref.get("host") if isinstance(work_ref, dict) else None
    if found is None or not isinstance(host, str) or not host:
        return None
    kind, key = found
    repo, ref = work_ref.get("repo"), work_ref.get("ref")
    if not isinstance(repo, str) or not isinstance(ref, str):
        return None
    number = ref[1:]
    if ref.startswith("!"):
        return f"https://{host}/{repo}/-/merge_requests/{number}"
    if "gitlab" in host:
        segment = "-/issues" if kind == "issue" else "-/merge_requests"
        return f"https://{host}/{repo}/{segment}/{number}"
    return f"https://{host}/{repo}/{'issues' if kind == 'issue' else 'pull'}/{number}"
