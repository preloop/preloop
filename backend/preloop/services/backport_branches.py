"""Branch naming rules for the backport flow (issue #961).

Kept free of other preloop imports so the flow schema can validate a
configuration with the same rules the runner uses.
"""

from __future__ import annotations

import re

MAX_BRANCH_NAME_LENGTH = 200
# Deliberately narrower than git-check-ref-format: release branch names are
# plain, and every name here ends up in a Git command line and a URL.
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")


def validate_branch_name(name: str) -> str:
    """Return ``name`` stripped, or raise when Git or a host would refuse it.

    Args:
        name: A branch name from the flow configuration.

    Returns:
        The stripped branch name.

    Raises:
        ValueError: The name is empty, too long, or not a plain branch name.
    """
    value = (name or "").strip()
    if not value:
        raise ValueError("branch names may not be empty")
    if len(value) > MAX_BRANCH_NAME_LENGTH:
        raise ValueError(
            f"branch name '{value[:40]}...' exceeds {MAX_BRANCH_NAME_LENGTH} characters"
        )
    if (
        not _BRANCH_PATTERN.match(value)
        or ".." in value
        or "//" in value
        or "/." in value
        or value.endswith(("/", ".", ".lock"))
        or value == "HEAD"
    ):
        raise ValueError(f"'{value}' is not a valid branch name")
    return value


def branch_component(target_branch: str) -> str:
    """Flatten a target branch into one branch name component.

    ``release/2.4`` becomes ``release-2.4`` so the backport branch does not
    nest under a directory that might collide with an existing branch.
    """
    return target_branch.replace("/", "-")


def backport_branch_name(pull_request_number: int, target_branch: str) -> str:
    """Name of the branch that carries one backport.

    The original pull request number and the target are both in the name, so
    a re-delivered event, or a retry after a failure, finds the same branch.

    Args:
        pull_request_number: Number (GitHub) or iid (GitLab) of the original.
        target_branch: The branch the change is backported to.

    Returns:
        For example ``backport/pr-812-to-release-2.4``.
    """
    return (
        f"backport/pr-{int(pull_request_number)}-to-{branch_component(target_branch)}"
    )
