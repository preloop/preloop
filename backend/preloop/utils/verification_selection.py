"""Required-check selection and publication decision (issue #428).

Pure-stdlib on purpose: this module's *source text* is embedded into the
runner-controlled verifier script that runs inside agent containers (see
``preloop.agents.verification``), where only python3 and the standard library
exist. Keep it free of third-party imports so the embedded copy and this
module can never drift apart — there is exactly one selection
implementation and one fail-closed publication decision, and both the
typed contract (``preloop.services.verification``) and the in-container
verifier execute them.

The shape of everything here is plain dicts/lists matching the
``VerificationProfile`` JSON schema.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Identity of the runner-controlled verifier. Evidence produced by anything
# else (the agent, a hand-written file) is refused by the gate.
VERIFICATION_PRODUCER = "preloop-verifier"
# Bumped when evidence semantics change incompatibly. The in-container
# verifier must agree with this value.
VERIFIER_VERSION = 1

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


def evaluate_from_raw(
    evidence: Optional[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any],
    commit_sha: str,
    tree_hash: str,
    clean_tree: bool = True,
    changed_files: Sequence[str] = (),
) -> Dict[str, Any]:
    """Fail-closed publication decision on plain dict evidence.

    Returns ``{"allowed": bool, "status": "passed"|"failed"|"blocked",
    "reason": str}``. Missing, failed, foreign-produced, wrong-commit or
    stale evidence refuses publication; only evidence for the current
    commit and tree allows it.

    The typed runner wrapper is
    :func:`preloop.services.verification.evaluate_publication`; the
    in-container verifier calls this function before writing ALLOW.
    """

    def deny(reason: str, status: str = "blocked") -> Dict[str, Any]:
        return {"allowed": False, "status": status, "reason": reason}

    if not evidence:
        return deny("verification evidence missing")
    if not isinstance(evidence, Mapping):
        return deny("verification evidence is malformed")

    producer = evidence.get("producer")
    verifier_version = evidence.get("verifier_version")
    if producer != VERIFICATION_PRODUCER or verifier_version != VERIFIER_VERSION:
        return deny("unexpected verifier identity/version")
    if (evidence.get("profile_id"), evidence.get("profile_version")) != (
        profile.get("profile_id"),
        str(profile.get("version", "")),
    ):
        return deny("stale verification profile")
    if (
        evidence.get("commit_sha") != commit_sha
        or evidence.get("tree_hash") != tree_hash
    ):
        return deny("verification evidence belongs to another commit/tree")
    if not clean_tree or not evidence.get("clean_tree"):
        return deny("tracked working tree changed after verification")

    selected = select_from_raw(profile, changed_files)
    commands = [entry["command"] for entry in selected["checks"]]
    if not commands:
        return deny("trusted profile selected no required checks")

    records_list = evidence.get("checks")
    if not isinstance(records_list, list):
        return deny("verification evidence is malformed")
    records: Dict[str, Mapping[str, Any]] = {}
    for record in records_list:
        if not isinstance(record, Mapping):
            return deny("verification evidence is malformed")
        rec_id = record.get("id")
        if rec_id in records:
            return deny("duplicate verification check records")
        records[str(rec_id)] = record
    if len(records) != len(records_list):
        return deny("duplicate verification check records")

    for requirement in commands:
        rec_id = str(requirement.get("id"))
        record = records.get(rec_id)
        if record is None or record.get("command") != requirement.get("command"):
            return deny("required check missing or command changed: " + rec_id)
        if record.get("exit_code") is None or record.get("skipped_reason"):
            return deny("required check unavailable/skipped: " + rec_id)
        if record.get("exit_code") != 0:
            return deny("required check failed: " + rec_id, "failed")

    if evidence.get("status") != "passed":
        ev_status = evidence.get("status")
        return deny(
            "verification run did not pass",
            "failed" if ev_status == "failed" else "blocked",
        )
    return {
        "allowed": True,
        "status": "passed",
        "reason": "all required checks passed for the final commit/tree",
    }
