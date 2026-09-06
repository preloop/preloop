"""Required-check selection for the publication gate (issue #428).

Pure-stdlib on purpose: this module's *source text* is embedded into the
runner-controlled verifier script that runs inside agent containers (see
``preloop.agents.verification``), where only python3 and the standard library
exist. Keep it free of third-party imports so the embedded copy and this
module can never drift apart — there is exactly one selection
implementation, and both the typed contract
(``preloop.services.verification``) and the in-container verifier execute it.

The shape of everything here is plain dicts/lists matching the
``VerificationProfile`` JSON schema.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, List, Mapping, Sequence

# selected_by markers for checks that are not tied to a changed-file rule.
SELECTED_BY_ALWAYS = "always"
SELECTED_BY_UNKNOWN_DEFAULT = "unknown_default"


def _match_any(path: str, patterns: Sequence[str]) -> bool:
    """fnmatch any pattern against the path (and its basename)."""

    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        normalized_pattern = str(pattern).replace("\\", "/").lstrip("./")
        if not normalized_pattern:
            continue
        if fnmatch.fnmatch(normalized, normalized_pattern):
            return True
        # 'Makefile' should match a rule written as '*/Makefile' and a
        # basename-only pattern should match at any depth.
        if fnmatch.fnmatch(normalized, f"*/{normalized_pattern}"):
            return True
        base = normalized.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(base, normalized_pattern):
            return True
    return False


def _add_selection(
    selections: List[Dict[str, Any]],
    by_id: Dict[str, Dict[str, Any]],
    command: Mapping[str, Any],
    selected_by: str,
) -> None:
    """Union-by-id: one check may be required by several rules."""

    existing = by_id.get(str(command.get("id")))
    if existing is not None:
        if selected_by not in existing["selected_by"]:
            existing["selected_by"].append(selected_by)
        return
    record = {
        "command": dict(command),
        "selected_by": [selected_by],
    }
    by_id[str(command.get("id"))] = record
    selections.append(record)


def select_from_raw(
    profile: Mapping[str, Any], changed_files: Sequence[str]
) -> Dict[str, Any]:
    """Derive required checks for a changed-file list.

    Returns ``{"checks": [{"command": {...}, "selected_by": [...]}],
    "matched_rule_ids": [...], "used_unknown_default": bool}``.

    Inexpensive ``always`` hooks are required on every diff. Rules matched
    against the diff union their commands. When no rule matches (unknown
    impact) the profile's conservative ``unknown_default`` is required —
    unknown impact never resolves to an empty test list.
    """

    selections: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    matched_rule_ids: List[str] = []
    covered_files: set[str] = set()

    for command in profile.get("always") or []:
        _add_selection(selections, by_id, command, SELECTED_BY_ALWAYS)

    for rule in profile.get("rules") or []:
        patterns = rule.get("path_globs") or []
        hits = [path for path in changed_files if _match_any(path, patterns)]
        if not hits:
            continue
        covered_files.update(hits)
        matched_rule_ids.append(str(rule.get("id")))
        for command in rule.get("commands") or []:
            _add_selection(selections, by_id, command, str(rule.get("id")))

    used_unknown_default = False
    if not changed_files or any(path not in covered_files for path in changed_files):
        used_unknown_default = True
        for command in profile.get("unknown_default") or []:
            _add_selection(selections, by_id, command, SELECTED_BY_UNKNOWN_DEFAULT)

    return {
        "checks": selections,
        "matched_rule_ids": matched_rule_ids,
        "used_unknown_default": used_unknown_default,
    }
