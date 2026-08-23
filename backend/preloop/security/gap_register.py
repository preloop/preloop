"""Deterministic gap-register freeze and result.json validation.

The agent never self-grades the floor. Previous SHA+path finding rows are
a floor: dropping one without ``resolved`` plus a reason fails. Unclassified
scan rows fail. ``secrets_findings_count`` must equal the SHA+path row count.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ALLOWED_ITEM_STATUSES = frozenset({"met", "gap", "partial", "declared"})
ALLOWED_ROW_STATUSES = frozenset({"finding", "not_a_finding"})


class GapRegisterValidationError(ValueError):
    """Raised when a gap register fails the deterministic freeze checks."""

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(self.failures))


def _is_hex_sha(value: str) -> bool:
    if len(value) < 7:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value)


def sha_path_key(sha: str, path: str) -> Tuple[str, str]:
    """Normalize a SHA+path pair for set comparison."""
    return (sha.lower(), (path or "").strip())


def extract_sha_path_rows(
    rows: Optional[Iterable[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Return rows that participate in the SHA+path floor."""
    extracted: List[Dict[str, Any]] = []
    for row in rows or []:
        sha = str(row.get("sha") or "").strip()
        path = str(row.get("path") or "").strip()
        if not sha or not _is_hex_sha(sha):
            continue
        extracted.append(dict(row))
        _ = path  # path may be empty for some history hits; still keyed
    return extracted


def secrets_findings_count(rows: Optional[Iterable[Mapping[str, Any]]]) -> int:
    """Count SHA+path rows whose status is finding (the register floor)."""
    count = 0
    for row in extract_sha_path_rows(rows):
        status = str(row.get("status") or "finding")
        if status == "finding":
            count += 1
    return count


def classify_rows(
    rows: Optional[Iterable[Mapping[str, Any]]],
) -> List[str]:
    """Return failures for unclassified or invalid row statuses."""
    failures: List[str] = []
    for idx, row in enumerate(rows or []):
        status = row.get("status")
        if status is None:
            failures.append(f"row[{idx}] unclassified (missing status)")
            continue
        if status not in ALLOWED_ROW_STATUSES:
            failures.append(f"row[{idx}] invalid status {status!r}")
            continue
        if status == "not_a_finding" and not str(row.get("reason") or "").strip():
            failures.append(f"row[{idx}] not_a_finding missing reason")
    return failures


def _previous_floor(previous: Mapping[str, Any]) -> Set[Tuple[str, str]]:
    floor: Set[Tuple[str, str]] = set()
    # Prefer an explicit secrets_findings list; fall back to items evidence
    # and any nested history rows.
    for row in previous.get("secrets_findings") or previous.get("history_rows") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("status") or "finding") != "finding":
            continue
        sha = str(row.get("sha") or "").strip()
        path = str(row.get("path") or "").strip()
        if sha and _is_hex_sha(sha):
            floor.add(sha_path_key(sha, path))
    return floor


def _current_finding_keys(
    current: Mapping[str, Any],
) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for row in current.get("secrets_findings") or current.get("history_rows") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("status") or "finding") != "finding":
            continue
        sha = str(row.get("sha") or "").strip()
        path = str(row.get("path") or "").strip()
        if sha and _is_hex_sha(sha):
            keys.add(sha_path_key(sha, path))
    return keys


def _resolved_keys(current: Mapping[str, Any]) -> Dict[Tuple[str, str], str]:
    resolved: Dict[Tuple[str, str], str] = {}
    for row in current.get("resolved") or []:
        if not isinstance(row, Mapping):
            continue
        sha = str(row.get("sha") or "").strip()
        path = str(row.get("path") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if sha and _is_hex_sha(sha) and reason:
            resolved[sha_path_key(sha, path)] = reason
    return resolved


def validate_gap_register(
    result: Mapping[str, Any],
    *,
    previous: Optional[Mapping[str, Any]] = None,
    repo_pin: Optional[str] = None,
    scan_rows: Optional[Iterable[Mapping[str, Any]]] = None,
) -> List[str]:
    """Validate a release-audit ``gap_register`` section.

    Args:
        result: Parsed ``result.json`` (or just the ``gap_register`` object).
        previous: Previous run's ``gap_register`` (the freeze floor).
        repo_pin: Expected 40-hex (or unique prefix) commit the register
            is keyed to. When set, ``gap_register.repo.commit`` must match.
        scan_rows: Optional raw tool rows that must all be classified.

    Returns:
        A list of failure strings. Empty means the register passed.
    """
    failures: List[str] = []
    register = result.get("gap_register") if "gap_register" in result else result
    if register is None:
        return failures
    if not isinstance(register, Mapping):
        return ["gap_register is not an object"]

    if repo_pin:
        commit = str((register.get("repo") or {}).get("commit") or "")
        pin = repo_pin.lower()
        if not commit.lower().startswith(pin[:12]) and pin[:12] not in commit.lower():
            failures.append(
                f"gap_register.repo.commit {commit!r} does not match pin {repo_pin[:12]}"
            )

    items = register.get("items") or []
    if not isinstance(items, list):
        failures.append("gap_register.items must be a list")
        items = []
    for idx, item in enumerate(items):
        if not isinstance(item, Mapping):
            failures.append(f"gap_register.items[{idx}] is not an object")
            continue
        status = item.get("status")
        if status not in ALLOWED_ITEM_STATUSES:
            failures.append(
                f"gap_register.items[{idx}].status must be met|gap|partial|declared"
            )

    not_checkable = register.get("not_checkable")
    if not_checkable is None:
        failures.append("gap_register.not_checkable is required")
    elif not isinstance(not_checkable, list):
        failures.append("gap_register.not_checkable must be a list")

    history_rows = (
        register.get("secrets_findings") or register.get("history_rows") or []
    )
    failures.extend(classify_rows(history_rows))
    if scan_rows is not None:
        failures.extend(classify_rows(scan_rows))

    expected_count = secrets_findings_count(history_rows)
    reported = register.get("secrets_findings_count")
    if reported is None:
        failures.append("gap_register.secrets_findings_count is required")
    elif int(reported) != expected_count:
        failures.append(
            "gap_register.secrets_findings_count "
            f"({reported}) != SHA+path finding row count ({expected_count})"
        )

    sbom = (
        result.get("sbom_audit")
        if isinstance(result.get("sbom_audit"), Mapping)
        else None
    )
    if sbom and sbom.get("verdict") == "fail" and result.get("verdict") != "fail":
        failures.append("sbom_audit.verdict is fail so result.verdict must be fail")

    if previous:
        prev_reg = (
            previous.get("gap_register") if "gap_register" in previous else previous
        )
        if isinstance(prev_reg, Mapping):
            floor = _previous_floor(prev_reg)
            current_keys = _current_finding_keys(register)
            resolved = _resolved_keys(register)
            for key in sorted(floor):
                if key in current_keys:
                    continue
                if key in resolved:
                    continue
                sha, path = key
                failures.append(
                    f"dropped prior SHA+path {sha[:12]} {path} without resolved+reason"
                )

    return failures


def assert_gap_register(
    result: Mapping[str, Any],
    *,
    previous: Optional[Mapping[str, Any]] = None,
    repo_pin: Optional[str] = None,
    scan_rows: Optional[Iterable[Mapping[str, Any]]] = None,
) -> None:
    """Raise :class:`GapRegisterValidationError` if validation fails."""
    failures = validate_gap_register(
        result,
        previous=previous,
        repo_pin=repo_pin,
        scan_rows=scan_rows,
    )
    if failures:
        raise GapRegisterValidationError(failures)
