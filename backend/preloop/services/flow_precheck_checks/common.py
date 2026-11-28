"""Helpers shared by the built-in pre-check types."""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Tuple

from preloop.services.flow_precheck import CheckResult


def folded(values: Iterable[str]) -> set[str]:
    """Case-folded set of names, for label and login comparisons."""
    return {str(value).casefold() for value in values}


def labels_match(labels: Sequence[str], condition: Any) -> bool:
    """Whether ``labels`` satisfy a ``PrecheckLabelFilter``."""
    have = folded(labels)
    if condition.any and not have & folded(condition.any):
        return False
    if condition.all and not folded(condition.all) <= have:
        return False
    if condition.none and have & folded(condition.none):
        return False
    return True


def bounded(
    items: List[dict], *, max_items: int, more_upstream: bool
) -> Tuple[List[dict], bool]:
    """The first ``max_items`` items, and whether anything was cut."""
    return items[:max_items], more_upstream or len(items) > max_items


def result(
    *,
    found: List[dict],
    min_count: int,
    max_items: int,
    more_upstream: bool,
    summary: str,
) -> CheckResult:
    """Build a :class:`CheckResult` from every item a check found."""
    items, truncated = bounded(found, max_items=max_items, more_upstream=more_upstream)
    return CheckResult(
        matched=len(found) >= min_count,
        count=len(found),
        items=items,
        truncated=truncated,
        summary=summary,
    )


def plural(count: int, singular: str, plural_form: str = "") -> str:
    """``"1 pull request"`` / ``"3 pull requests"``."""
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count} {word}"
