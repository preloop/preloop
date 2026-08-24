"""Deterministic waiver application for security-audit gates.

Waivers are the governed alternative to verdict upgrades: a human accepts
a specific failing gate item, on the record, and the gate is recomputed
around that acceptance. The rules are deterministic and enforced here, not
by the model:

- Only human-authored waiver entries count. An entry must carry the finding
  or gate-family id it waives, a non-empty free-text reason, an author, and
  a date. Entries collected interactively must also carry the platform
  approval id that captured the approver's identity.
- A waiver never upgrades anything by itself: it only removes the item it
  names from the set of gate failures. An unwaived failure keeps the gate
  failed, and a failed SBOM audit stays a failed audit no matter how many
  waivers exist.
- Every applied waiver is part of the evidence: callers echo entries
  verbatim into the evidence pack and list them on the cover page.

Like ``gap_register``, this module is the deterministic reference half:
presets instruct the agent to apply the same rules, and result ingestion
can re-validate a submitted gate against the delivered waiver file.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

#: A gate item that failed the severity policy: either a bare finding id
#: (``"CVE-2021-44228"``) or a mapping ``{"id": ..., "aliases": [...]}`` so a
#: CVE-id waiver matches the same advisory surfaced under a GHSA/OSV alias.
FailingItem = Union[str, Mapping[str, Any]]

#: Fields a waiver entry must carry to be applicable at all.
REQUIRED_WAIVER_FIELDS = ("id", "reason", "author", "date")

#: The only collection modes the presets recognize.
ALLOWED_COLLECTION_MODES = frozenset({"file", "interactive"})


class WaiverValidationError(ValueError):
    """Raised when waiver entries or their application fail validation."""

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(self.failures))


def normalize_waiver_id(value: Any) -> str:
    """Normalize a finding/gate-family id for comparison."""
    return str(value or "").strip().lower()


def validate_waiver_entries(
    entries: Optional[Iterable[Mapping[str, Any]]],
    *,
    require_approval_id: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split waiver entries into (valid, failures).

    Args:
        entries: Raw waiver entries from the payload file or the
            interactive transcript.
        require_approval_id: When True (interactive collection), an entry
            without a platform ``approval_id`` is invalid — the approval
            record is what captures the approver's identity.

    Returns:
        Tuple of (valid entries as dicts, human-readable failure strings).
    """
    valid: List[Dict[str, Any]] = []
    failures: List[str] = []
    seen: set = set()
    for index, raw in enumerate(entries or []):
        if not isinstance(raw, Mapping):
            failures.append(f"waiver[{index}]: entry is not an object")
            continue
        entry = dict(raw)
        missing = [
            field
            for field in REQUIRED_WAIVER_FIELDS
            if not str(entry.get(field) or "").strip()
        ]
        if missing:
            failures.append(
                f"waiver[{index}] ({entry.get('id', '?')}): missing "
                + ", ".join(missing)
            )
            continue
        if require_approval_id and not str(entry.get("approval_id") or "").strip():
            failures.append(
                f"waiver[{index}] ({entry['id']}): interactive waiver "
                "without an approval_id"
            )
            continue
        key = normalize_waiver_id(entry["id"])
        if key in seen:
            failures.append(f"waiver[{index}] ({entry['id']}): duplicate id")
            continue
        seen.add(key)
        valid.append(entry)
    return valid, failures


def _failing_keys(item: Any) -> Tuple[str, frozenset]:
    """Return (display id, normalized {id} | aliases) for a failing item.

    A failing item is either a bare id string or a mapping with ``id`` and
    optional ``aliases`` — a CVE-id waiver must match the same advisory
    surfaced under a GHSA/OSV alias.
    """
    if isinstance(item, Mapping):
        display = str(item.get("id") or "")
        keys = {normalize_waiver_id(item.get("id"))}
        keys.update(normalize_waiver_id(a) for a in item.get("aliases") or [])
    else:
        display = str(item)
        keys = {normalize_waiver_id(item)}
    keys.discard("")
    return display, frozenset(keys)


def apply_waivers(
    failing_ids: Sequence[FailingItem],
    waivers: Optional[Iterable[Mapping[str, Any]]],
    *,
    require_approval_id: bool = False,
) -> Dict[str, Any]:
    """Deterministically factor waivers into a gate outcome.

    Args:
        failing_ids: The gate items that failed the severity policy —
            finding ids such as CVE ids, gate-family ids, or mappings of
            ``{"id": ..., "aliases": [...]}`` for alias-aware matching.
        waivers: Candidate waiver entries.
        require_approval_id: Passed through to validation (interactive
            collection requires the platform approval id per entry).

    Returns:
        Dict with:
        - ``gate_passed_after_waivers``: True only when every failing id is
          covered by a valid waiver. No failures also passes.
        - ``waivers_applied``: the valid entries that matched a failing id,
          echoed verbatim (list of dicts).
        - ``unwaived_failures``: failing ids no valid waiver covers.
        - ``waivers_unmatched``: valid entries that matched nothing (echoed
          so a stale waiver is visible, never silently dropped).
        - ``invalid_entries``: human-readable strings for rejected entries.
    """
    valid, invalid = validate_waiver_entries(
        waivers, require_approval_id=require_approval_id
    )
    by_id = {normalize_waiver_id(entry["id"]): entry for entry in valid}
    applied: List[Dict[str, Any]] = []
    unwaived: List[str] = []
    matched_keys: set = set()
    for failing in failing_ids:
        display, keys = _failing_keys(failing)
        entry = next((by_id[k] for k in sorted(keys) if k in by_id), None)
        if entry is None:
            unwaived.append(display)
            continue
        key = normalize_waiver_id(entry["id"])
        if key not in matched_keys:
            matched_keys.add(key)
            applied.append(entry)
    unmatched = [
        entry for entry in valid if normalize_waiver_id(entry["id"]) not in matched_keys
    ]
    return {
        "gate_passed_after_waivers": not unwaived,
        "waivers_applied": applied,
        "unwaived_failures": unwaived,
        "waivers_unmatched": unmatched,
        "invalid_entries": invalid,
    }


def validate_waived_gate(
    gate: Mapping[str, Any],
    failing_ids: Sequence[FailingItem],
    waivers: Optional[Iterable[Mapping[str, Any]]],
    *,
    require_approval_id: bool = False,
) -> List[str]:
    """Re-validate a submitted gate object against the waiver inputs.

    The agent never self-grades a waived gate: given the failing ids and
    the delivered waiver entries, the submitted ``gate.passed`` and
    ``gate.waivers_applied`` must equal this module's deterministic
    outcome.

    Returns:
        List of failure strings; empty when the gate is consistent.
    """
    failures: List[str] = []
    outcome = apply_waivers(
        failing_ids, waivers, require_approval_id=require_approval_id
    )
    submitted_passed = bool(gate.get("passed"))
    if submitted_passed != outcome["gate_passed_after_waivers"]:
        failures.append(
            "gate.passed="
            f"{submitted_passed} but deterministic waiver application says "
            f"{outcome['gate_passed_after_waivers']} "
            f"(unwaived: {outcome['unwaived_failures']})"
        )
    submitted_applied = {
        normalize_waiver_id(entry.get("id"))
        for entry in gate.get("waivers_applied") or []
    }
    expected_applied = {
        normalize_waiver_id(entry["id"]) for entry in outcome["waivers_applied"]
    }
    if submitted_applied != expected_applied:
        failures.append(
            f"gate.waivers_applied ids {sorted(submitted_applied)} != "
            f"deterministic set {sorted(expected_applied)}"
        )
    return failures


def assert_waived_gate(
    gate: Mapping[str, Any],
    failing_ids: Sequence[FailingItem],
    waivers: Optional[Iterable[Mapping[str, Any]]],
    *,
    require_approval_id: bool = False,
) -> None:
    """Raise :class:`WaiverValidationError` when the gate is inconsistent."""
    failures = validate_waived_gate(
        gate, failing_ids, waivers, require_approval_id=require_approval_id
    )
    if failures:
        raise WaiverValidationError(failures)
