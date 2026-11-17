"""Flow configuration revisions as HTTP entity tags (#2375).

A flow's ``config_revision`` is exposed as the strong entity tag
``"<revision>"``. Writes and triggers accept ``If-Match`` (RFC 9110 13.1.1):
``*`` or a list of entity tags. Weak tags never match, because ``If-Match``
uses the strong comparison.
"""

import re
from dataclasses import dataclass
from typing import Optional

# One list member: optional weak prefix, quoted opaque tag, then a comma or end.
_ENTITY_TAG = re.compile(r'\s*(W/)?"([\x21\x23-\x7e\x80-\xff]*)"\s*(?:,|$)')


def flow_etag(revision: int) -> str:
    """Return the strong entity tag for a configuration revision.

    Args:
        revision: The flow's ``config_revision``.

    Returns:
        The quoted tag, for example ``"7"``.
    """
    return f'"{revision}"'


class MalformedIfMatchError(ValueError):
    """The ``If-Match`` header is not ``*`` or a list of entity tags."""


class FlowRevisionMismatchError(Exception):
    """A precondition named a revision other than the flow's current one.

    Attributes:
        current_revision: The revision the flow has now.
    """

    def __init__(self, current_revision: int) -> None:
        """Record the current revision for the 412 response.

        Args:
            current_revision: The revision the flow has now.
        """
        super().__init__(f"Flow is at revision {current_revision}")
        self.current_revision = current_revision


@dataclass(frozen=True)
class RevisionCondition:
    """A parsed ``If-Match`` precondition.

    Attributes:
        any_revision: ``If-Match: *``: the flow must exist, any revision.
        strong_tags: The strong entity tags listed, quotes included.
    """

    any_revision: bool
    strong_tags: frozenset[str]

    def matches(self, revision: int) -> bool:
        """Apply the strong comparison to the flow's current revision.

        Args:
            revision: The flow's current ``config_revision``.

        Returns:
            True when the write or trigger may proceed.
        """
        return self.any_revision or flow_etag(revision) in self.strong_tags

    def require(self, revision: int) -> None:
        """Raise unless the condition holds for ``revision``.

        Args:
            revision: The flow's current ``config_revision``.

        Raises:
            FlowRevisionMismatchError: The condition names another revision.
        """
        if not self.matches(revision):
            raise FlowRevisionMismatchError(revision)


def parse_if_match(value: Optional[str]) -> Optional[RevisionCondition]:
    """Parse an ``If-Match`` header value.

    Args:
        value: The raw header, or None when the request has none.

    Returns:
        None without a header (the write is unconditional), otherwise the
        condition.

    Raises:
        MalformedIfMatchError: The value is empty or not ``*`` or a list of
            entity tags.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "*":
        return RevisionCondition(any_revision=True, strong_tags=frozenset())
    if not text:
        raise MalformedIfMatchError("If-Match must be * or a list of entity tags")
    strong: set[str] = set()
    position = 0
    while position < len(text):
        match = _ENTITY_TAG.match(text, position)
        if match is None or match.end() == position:
            raise MalformedIfMatchError(
                'If-Match must be * or a list of entity tags such as "7"'
            )
        if not match.group(1):
            strong.add(f'"{match.group(2)}"')
        position = match.end()
    if text.endswith(","):
        raise MalformedIfMatchError("If-Match ends with an empty list member")
    return RevisionCondition(any_revision=False, strong_tags=frozenset(strong))
