"""Publish a generated report through the flow's pull request path (issue #648).

A review flow that reads many untrusted projects must stay read only: the
agent gets no write tools, no push credentials in its tool surface and no
provider calls it can author. The report it produces is still worth more in
the repository than in an artifact nobody opens, so the document leaves the
run the same way every other change does, through the platform's own pull
request path, and it leaves *after* the agent has exited.

The shell built here therefore runs in the post-execution block, in a
throwaway git worktree of the checkout:

- the destination document is the only path ever staged or committed, so a
  workspace an untrusted repository left dirty cannot ride along;
- the branch is stable per document (see :func:`report_branch_name`), so a
  re-run updates the open pull request instead of opening a second one;
- a byte identical document commits nothing, pushes nothing and opens
  nothing, and says so;
- the default branch is never a commit or push target, which is what keeps a
  protected default branch from turning into a refused direct commit;
- every failure degrades: the block cannot exit non-zero, the report artifact
  is untouched, and the reason is printed on the marker line for the result.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# One line the post-execution block prints, always exactly once:
#   PRELOOP_REPORT_PUBLICATION {"outcome": "...", "reason": "...", ...}
REPORT_PUBLICATION_MARKER = "PRELOOP_REPORT_PUBLICATION"

# Result key the control plane owns. Agent result.json cannot author it (see
# preloop.services.flow_artifacts.RESERVED_RESULT_FIELDS).
REPORT_PUBLICATION_RESULT_KEY = "report_publication"

# Where the publication worktree and its log live. Both are outside the
# checkout: the agent's workspace is never written to by this block.
REPORT_PUBLICATION_WORKTREE = "/tmp/preloop-report-publication"
REPORT_PUBLICATION_LOG = "/workspace/evidence/report-publication.log"
REPORT_PUBLICATION_LOG_RELATIVE = "evidence/report-publication.log"

# Every branch this module creates starts here, so an operator reading the
# repository can tell platform maintained report branches from human ones.
REPORT_BRANCH_PREFIX = "preloop/report"

OUTCOME_PUBLISHED = "published"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_FAILED = "failed"
REPORT_PUBLICATION_OUTCOMES = frozenset(
    {OUTCOME_PUBLISHED, OUTCOME_UNCHANGED, OUTCOME_FAILED}
)

# Closed vocabulary. A reason is a diagnosis an operator can act on, never a
# provider error string, so nothing unbounded (or secret bearing) reaches the
# marker line or the stored result.
REPORT_PUBLICATION_REASONS = frozenset(
    {
        "",  # published: nothing to explain
        "identical_document",
        "report_missing",
        "checkout_unavailable",
        "base_branch_unavailable",
        "worktree_failed",
        "copy_failed",
        "stage_failed",
        "commit_failed",
        "push_failed",
        "pull_request_unavailable",
        "pull_request_disabled",
        "provider_unsupported",
        "repository_missing",
        "repository_ambiguous",
        "invalid_configuration",
        "write_flow_conflict",
    }
)


def closed_vocabulary_member(value: object, allowed: frozenset[str]) -> Optional[str]:
    """Return the vocabulary string itself, never the caller-supplied copy.

    Equality against ``allowed`` is not enough for logging: the matching
    member of the frozenset is a constant, so a tainted agent line cannot
    ride into a log sink through the parsed JSON.
    """
    if not isinstance(value, str):
        return None
    for item in allowed:
        if value == item:
            return item
    return None


MAX_PATH_LENGTH = 255
MAX_COMMIT_MESSAGE_LENGTH = 512

_SAFE_GIT_REF_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
# Destination and source paths are interpolated into the post-execution
# marker. Anything outside this allowlist is a shell metacharacter (or
# worse) and is refused before it can reach echo or python -c.
_SHELL_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._/@ +-]")


class ReportPublicationError(ValueError):
    """Configuration this module refuses to turn into a publication.

    Carries a ``reason`` from the closed vocabulary so the caller can degrade
    the run with a diagnosis rather than an exception message.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validated_git_ref(name: Optional[str]) -> Optional[str]:
    """Return ``name`` when it is a safe git branch name, otherwise None.

    Deliberately the same rule the container applies to every other branch it
    interpolates into shell (``preloop.agents.container._validated_git_ref``);
    ``test_report_publication`` asserts the two agree so they cannot drift.
    """
    if not name or not isinstance(name, str):
        return None
    if not _SAFE_GIT_REF_CHARS.fullmatch(name):
        return None
    if name.startswith("-") or name.startswith("/") or name.endswith("."):
        return None
    if name.endswith("/") or ".." in name or "//" in name:
        return None
    for part in name.split("/"):
        if not part or part.startswith(".") or part.endswith(".lock"):
            return None
    return name


def validated_relative_path(value: Any) -> Optional[str]:
    """Return a repository relative path, or None when it is not one.

    Absolute paths, traversal, backslashes, control characters, shell
    metacharacters and anything under ``.git`` are refused: this path is
    a ``cp`` target, a git pathspec and a marker field, and it comes from
    flow configuration.
    """
    if not value or not isinstance(value, str):
        return None
    path = value.strip()
    if not path or len(path) > MAX_PATH_LENGTH:
        return None
    if path.startswith("/") or path.startswith("-") or "\\" in path:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        return None
    if _SHELL_UNSAFE_PATH.search(path):
        return None
    parts = path.split("/")
    for part in parts:
        if not part or part in {".", ".."}:
            return None
    if parts[0] == ".git":
        return None
    return path


def report_branch_name(destination_path: str, override: Optional[str] = None) -> str:
    """Stable branch for the document at ``destination_path``.

    The branch is keyed on the document, not on the execution: the same
    portfolio report always lands on ``preloop/report/<document slug>``, so a
    re-run pushes onto the branch the open pull request already tracks instead
    of opening a second one. Two different documents in one repository get two
    different branches, and therefore two independent pull requests.

    ``override`` (``report_publication.branch``) wins when it is a valid
    branch name, for operators whose repositories already have a naming
    convention.

    Raises:
        ReportPublicationError: when neither the override nor the derived name
            is a usable branch.
    """
    if override is not None and str(override).strip():
        validated = validated_git_ref(str(override).strip())
        if validated is None:
            raise ReportPublicationError(
                "invalid_configuration",
                f"report_publication.branch is not a valid branch name: {override!r}",
            )
        return validated
    stem = (
        destination_path.rsplit(".", 1)[0]
        if "." in destination_path
        else destination_path
    )
    slug = _SLUG_UNSAFE.sub("-", stem.lower()).strip("-")
    branch = validated_git_ref(f"{REPORT_BRANCH_PREFIX}/{slug}") if slug else None
    if branch is None:
        raise ReportPublicationError(
            "invalid_configuration",
            f"cannot derive a branch name from {destination_path!r}",
        )
    return branch


@dataclass(frozen=True)
class ReportPublicationPlan:
    """What to publish, where it lands, and on which branch."""

    source_path: str  # absolute path inside the container workspace
    destination_path: str  # repository relative path of the document
    branch: str  # stable branch this document is maintained on
    commit_message: str

    @property
    def destination_directory(self) -> Optional[str]:
        """Repository relative parent of the document, when it has one."""
        if "/" not in self.destination_path:
            return None
        return self.destination_path.rsplit("/", 1)[0]

    def as_marker_fields(self) -> dict[str, str]:
        """Fields every marker line carries, whatever the outcome."""
        return {"branch": self.branch, "document": self.destination_path}


def resolve_report_publication(
    git_config: Optional[Mapping[str, Any]],
) -> Optional[ReportPublicationPlan]:
    """Read ``git_clone_config.report_publication`` into a plan.

    Returns None when the flow does not publish a report, which is every flow
    that has not opted in.

    Raises:
        ReportPublicationError: when the block is present and enabled but
            unusable. The caller degrades the run with ``error.reason``
            rather than failing it.
    """
    if not isinstance(git_config, Mapping):
        return None
    block = git_config.get("report_publication")
    if not isinstance(block, Mapping) or not block.get("enabled"):
        return None

    source = validated_relative_path(block.get("source_path"))
    if source is None:
        raise ReportPublicationError(
            "invalid_configuration",
            "report_publication.source_path must be a workspace relative path",
        )
    destination = validated_relative_path(block.get("destination_path"))
    if destination is None:
        raise ReportPublicationError(
            "invalid_configuration",
            "report_publication.destination_path must be a repository relative path",
        )
    branch = report_branch_name(destination, block.get("branch"))

    raw_message = block.get("commit_message")
    message = str(raw_message).strip() if raw_message else ""
    if not message:
        message = f"Update {destination}"
    message = message.splitlines()[0][:MAX_COMMIT_MESSAGE_LENGTH]

    return ReportPublicationPlan(
        source_path=f"/workspace/{source}",
        destination_path=destination,
        branch=branch,
        commit_message=message,
    )


def _marker_print_shell(
    outcome_assignment: str,
    reason_assignment: str,
    fields: Mapping[str, str],
) -> str:
    """Print the marker via python so field values never enter a quoted echo.

    Outcome and reason are supplied as already-quoted shell assignments
    (literals, or expansions of the closed-vocabulary variables the
    publication block sets). Document and branch travel through the
    environment, the same way ``_build_pr_or_mr_create_shell`` encodes
    its payload: ``$`` / backticks / quotes in a flow-configured path
    cannot become command substitution.
    """
    env_parts = [outcome_assignment, reason_assignment]
    py_pairs = [
        '"outcome": os.environ.get("PRELOOP_REPORT_OUTCOME", "")',
        '"reason": os.environ.get("PRELOOP_REPORT_REASON", "")',
    ]
    for index, (key, value) in enumerate(sorted(fields.items())):
        env_name = f"PRELOOP_REPORT_FIELD_{index}"
        env_parts.append(f"{env_name}={shlex.quote(str(value))}")
        py_pairs.append(
            f"{json.dumps(key)}: os.environ.get({json.dumps(env_name)}, '')"
        )
    py_pairs.append(f'"log": {json.dumps(REPORT_PUBLICATION_LOG_RELATIVE)}')
    python = (
        "import json,os;"
        f"print({json.dumps(REPORT_PUBLICATION_MARKER + ' ')}+"
        "json.dumps({" + ",".join(py_pairs) + "},separators=(',',':')))"
    )
    return f"{' '.join(env_parts)} python3 -c {shlex.quote(python)}"


def _marker_echo(outcome_ref: str, reason_ref: str, fields: Mapping[str, str]) -> str:
    """Shell that prints the single marker line, with shell-expanded state."""
    return _marker_print_shell(
        f'PRELOOP_REPORT_OUTCOME="${{{outcome_ref}}}"',
        f'PRELOOP_REPORT_REASON="${{{reason_ref}}}"',
        fields,
    )


def build_failed_report_publication_shell(
    reason: str, fields: Optional[Mapping[str, str]] = None
) -> str:
    """Shell that only reports a refusal, for configuration that cannot run.

    Nothing is cloned, committed or pushed: a publication the platform refuses
    to attempt is still an outcome the run has to disclose.
    """
    if reason not in REPORT_PUBLICATION_REASONS:
        reason = "invalid_configuration"
    return _marker_print_shell(
        f"PRELOOP_REPORT_OUTCOME={shlex.quote(OUTCOME_FAILED)}",
        f"PRELOOP_REPORT_REASON={shlex.quote(reason)}",
        fields or {},
    )


def build_report_publication_shell(
    plan: ReportPublicationPlan,
    *,
    clone_path: str,
    base_branch: str,
    git_user_name: str,
    git_user_email: str,
    push_auth_shell: str,
    pull_request_shell: str,
) -> str:
    """Build the post-execution block that lands the document as a pull request.

    Args:
        plan: what to publish and where.
        clone_path: absolute path of the checkout the flow cloned.
        base_branch: the repository's default branch, used only as the start
            point of the report branch and as the pull request base. It is
            never checked out for writing, never committed to and never pushed.
        git_user_name: commit author name (``git_clone_config.git_user_name``).
        git_user_email: commit author email.
        push_auth_shell: credential helper reinstall emitted by the caller.
        pull_request_shell: the flow's existing create-or-find pull request
            block, built for head ``plan.branch`` and base ``base_branch``.

    Returns:
        Shell that always exits zero and always prints exactly one marker line.
    """
    safe_base = validated_git_ref(base_branch)
    if safe_base is None:
        return build_failed_report_publication_shell(
            "base_branch_unavailable", plan.as_marker_fields()
        )

    branch = plan.branch
    quoted_clone = shlex.quote(clone_path)
    quoted_source = shlex.quote(plan.source_path)
    quoted_destination = shlex.quote(plan.destination_path)
    quoted_worktree = shlex.quote(REPORT_PUBLICATION_WORKTREE)
    quoted_message = shlex.quote(plan.commit_message)
    quoted_name = shlex.quote(git_user_name or "Preloop")
    quoted_email = shlex.quote(git_user_email or "hello@preloop.ai")
    log = REPORT_PUBLICATION_LOG
    make_destination_dir = ""
    if plan.destination_directory is not None:
        make_destination_dir = f"  mkdir -p {shlex.quote(plan.destination_directory)} >>{log} 2>&1 || true\n"

    return f"""
PRELOOP_REPORT_OUTCOME={OUTCOME_FAILED}
PRELOOP_REPORT_REASON=invalid_configuration
mkdir -p {shlex.quote(REPORT_PUBLICATION_LOG.rsplit("/", 1)[0])} 2>/dev/null || true
_preloop_report_publish() {{
  if [ ! -f {quoted_source} ]; then
    PRELOOP_REPORT_REASON=report_missing
    return 1
  fi
  if ! cd {quoted_clone} 2>>{log}; then
    PRELOOP_REPORT_REASON=checkout_unavailable
    return 1
  fi
  if ! git rev-parse --git-dir >/dev/null 2>>{log}; then
    PRELOOP_REPORT_REASON=checkout_unavailable
    return 1
  fi
{push_auth_shell}
  # The stable report branch is the only ref this block writes. The default
  # branch is fetched read only, purely as the start point of a first run.
  git fetch --no-tags --quiet origin \
    '+refs/heads/{branch}:refs/remotes/origin/{branch}' >>{log} 2>&1 || true
  if git rev-parse --verify --quiet refs/remotes/origin/{branch} >/dev/null 2>&1; then
    PRELOOP_REPORT_START=refs/remotes/origin/{branch}
  else
    git fetch --no-tags --quiet origin \
      '+refs/heads/{safe_base}:refs/remotes/origin/{safe_base}' >>{log} 2>&1 || true
    if git rev-parse --verify --quiet refs/remotes/origin/{safe_base} >/dev/null 2>&1; then
      PRELOOP_REPORT_START=refs/remotes/origin/{safe_base}
    elif git rev-parse --verify --quiet {safe_base} >/dev/null 2>&1; then
      PRELOOP_REPORT_START={safe_base}
    else
      PRELOOP_REPORT_REASON=base_branch_unavailable
      return 1
    fi
  fi
  # A throwaway worktree, so a checkout the agent left dirty (it read many
  # untrusted projects) cannot contribute a single byte to this commit.
  rm -rf {quoted_worktree} >>{log} 2>&1 || true
  git worktree prune >>{log} 2>&1 || true
  if ! git worktree add --force -B {branch} {quoted_worktree} \
    "$PRELOOP_REPORT_START" >>{log} 2>&1; then
    PRELOOP_REPORT_REASON=worktree_failed
    return 1
  fi
  if ! cd {quoted_worktree} 2>>{log}; then
    PRELOOP_REPORT_REASON=worktree_failed
    return 1
  fi
{make_destination_dir}  if ! cp {quoted_source} {quoted_destination} >>{log} 2>&1; then
    PRELOOP_REPORT_REASON=copy_failed
    return 1
  fi
  if ! git add -- {quoted_destination} >>{log} 2>&1; then
    PRELOOP_REPORT_REASON=stage_failed
    return 1
  fi
  if git diff --cached --quiet -- {quoted_destination}; then
    PRELOOP_REPORT_OUTCOME={OUTCOME_UNCHANGED}
    PRELOOP_REPORT_REASON=identical_document
    echo "Report is byte identical to {branch}; nothing to publish"
    return 0
  fi
  # One pathspec, so the commit carries exactly one changed file whatever
  # else is in the tree.
  if ! git -c user.name={quoted_name} -c user.email={quoted_email} \
    commit -q -m {quoted_message} -- {quoted_destination} >>{log} 2>&1; then
    PRELOOP_REPORT_REASON=commit_failed
    return 1
  fi
  COMMIT_COUNT=1
  if ! git push origin "HEAD:refs/heads/{branch}" >>{log} 2>&1; then
    PRELOOP_REPORT_REASON=push_failed
    return 1
  fi
  PRELOOP_REPORT_OUTCOME={OUTCOME_PUBLISHED}
  PRELOOP_REPORT_REASON=
{pull_request_shell}
  # The embedded capture shell sets this flag and keeps going. A bare exit
  # there would skip this marker. The body update failed after the push.
  if [ -n "${{PRELOOP_PROVENANCE_FAILED:-}}" ]; then
    PRELOOP_REPORT_OUTCOME={OUTCOME_FAILED}
    PRELOOP_REPORT_REASON=pull_request_unavailable
  elif [ -z "${{PR_URL:-}}${{MR_URL:-}}" ]; then
    PRELOOP_REPORT_OUTCOME={OUTCOME_FAILED}
    PRELOOP_REPORT_REASON=pull_request_unavailable
  fi
  return 0
}}
_preloop_report_publish || echo "Report publication did not complete: $PRELOOP_REPORT_REASON"
cd /workspace 2>/dev/null || true
{_marker_echo("PRELOOP_REPORT_OUTCOME", "PRELOOP_REPORT_REASON", plan.as_marker_fields())}
""".lstrip()


def parse_report_publication_marker(line: str) -> Optional[dict[str, Any]]:
    """Parse the marker line into the record the execution result carries.

    Only the closed vocabularies are accepted: a look-alike line an agent
    printed cannot claim a publication that did not happen.
    """
    if not isinstance(line, str):
        return None
    stripped = line.strip()
    if not stripped.startswith(REPORT_PUBLICATION_MARKER + " "):
        return None
    payload = stripped.split(" ", 1)[1].strip()
    try:
        parsed = json.loads(payload)
    except ValueError:
        logger.warning("Ignoring malformed %s marker", REPORT_PUBLICATION_MARKER)
        return None
    if not isinstance(parsed, dict):
        return None
    outcome = closed_vocabulary_member(
        parsed.get("outcome"), REPORT_PUBLICATION_OUTCOMES
    )
    if outcome is None:
        logger.warning(
            "Ignoring %s marker with unknown outcome", REPORT_PUBLICATION_MARKER
        )
        return None
    reason = closed_vocabulary_member(parsed.get("reason"), REPORT_PUBLICATION_REASONS)
    if reason is None:
        return None
    record: dict[str, Any] = {"outcome": outcome, "reason": reason}
    for key in ("branch", "document", "log"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            record[key] = value
    return record
