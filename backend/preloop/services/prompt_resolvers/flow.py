"""Resolver for flow-owned prompt fields."""

from __future__ import annotations

from typing import Optional

from preloop.models.crud import crud_flow

from .base import PromptResolver, ResolverContext


def review_instructions_text(value: Optional[str]) -> str:
    """Return the prompt text for a stored review_instructions value.

    Args:
        value: Column value. None and whitespace mean the flow has no
            extra review rules.

    Returns:
        Stripped instructions, or an empty string when unset. An empty
        string is deliberate: the placeholder is replaced, so the agent
        does not see a raw ``{{flow.review_instructions}}`` token.
    """

    if value is None:
        return ""
    return value.strip()


class FlowResolver(PromptResolver):
    """Placeholders for the flow row that owns this execution.

    - ``{{flow.review_instructions}}``: blocking review rules, empty when
      the flow has none. The Pull Request Reviewer treats this the same
      way it treats ``.preloop/review-policy.md``.
    """

    @property
    def prefix(self) -> str:
        """Return the placeholder prefix this resolver handles."""
        return "flow"

    async def resolve(self, path: str, context: ResolverContext) -> Optional[str]:
        """Resolve a flow field.

        Args:
            path: Path after the prefix. Only ``review_instructions`` is
                defined.
            context: Resolver context. ``flow_id`` selects the row.

        Returns:
            The field text, or None when the field name is unknown.
        """

        if path != "review_instructions":
            self.logger.warning("Unknown flow field: %s", path)
            return None
        flow = crud_flow.get(context.db, id=context.flow_id)
        if flow is None:
            return ""
        return review_instructions_text(getattr(flow, "review_instructions", None))
