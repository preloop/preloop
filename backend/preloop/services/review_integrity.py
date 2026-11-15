"""Controller checks that a read-only review result describes what was reviewed.

Two checks run on every restricted-CI read-only review that would otherwise
succeed, and again before Preloop posts anything on the pull request:

- **Result binding.** Result fields that name the reviewed revision must
  equal what the controller admitted (``ci_review_binding``) and computed
  (the startup receipt's ``diff``). A schema-valid result whose ``head_sha``
  is all zeros, missing or another commit is not a review of this change.
- **Evidence floor.** A review that made no repository tool call and ran no
  command did not read the change, whatever its verdict says. "Inspected" is
  decided only from tool evidence Preloop captured itself: gateway-observed
  tool calls and native command events, never from the model's claims.

Both are on by default for read-only reviews. ``git_clone_config.
review_integrity`` can name explicit bindings or turn the evidence floor off.
"""

import re
from collections.abc import Mapping
from typing import Any

#: Controller facts a result field can be bound to.
FACT_HEAD = "head_sha"
FACT_BASE = "base_sha"
FACT_MERGE_BASE = "merge_base_sha"
FACT_DIFF = "diff_sha256"
FACTS: frozenset[str] = frozenset({FACT_HEAD, FACT_BASE, FACT_MERGE_BASE, FACT_DIFF})

#: Default bindings by result field name. A default binding is enforced when
#: the flow's result schema declares the field or the result contains it.
DEFAULT_BINDINGS: dict[str, str] = {
    "head_sha": FACT_HEAD,
    "base_sha": FACT_BASE,
    "merge_base_sha": FACT_MERGE_BASE,
    "diff_sha256": FACT_DIFF,
}

CATEGORY_BINDING_MISMATCH = "review_binding_mismatch"
CATEGORY_NOT_INSPECTED = "review_not_inspected"

STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"
STATUS_NOT_APPLICABLE = "not_applicable"

#: Native tools that never read the repository. Any other tool call (a
#: shell, ``exec_command``, a file read, grep) counts as inspection.
NON_INSPECTING_TOOLS: frozenset[str] = frozenset(
    {
        "request_user_input",
        "view_image",
        "update_plan",
        "get_goal",
        "create_goal",
        "update_goal",
        "multi_agent_v1",
        "web_search",
    }
)

_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def review_integrity_block(config: Any) -> dict[str, Any]:
    """The flow's ``review_integrity`` settings; empty when unset."""
    block = config.get("review_integrity") if isinstance(config, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else {}


def validate_review_integrity_block(block: Any) -> None:
    """Reject an unknown fact or a malformed field name.

    Raises:
        ValueError: When the block is not a valid configuration.
    """
    if block is None:
        return
    if not isinstance(block, Mapping):
        raise ValueError("review_integrity must be an object")
    unknown = set(block) - {"result_bindings", "require_inspection"}
    if unknown:
        raise ValueError(f"review_integrity has unknown key {sorted(unknown)[0]!r}")
    inspection = block.get("require_inspection")
    if inspection is not None and not isinstance(inspection, bool):
        raise ValueError("review_integrity.require_inspection must be a boolean")
    bindings = block.get("result_bindings")
    if bindings is None:
        return
    if not isinstance(bindings, Mapping) or not all(
        isinstance(field, str)
        and _FIELD_RE.fullmatch(field)
        and isinstance(fact, str)
        and fact in FACTS
        for field, fact in bindings.items()
    ):
        raise ValueError(
            "review_integrity.result_bindings maps result field names to "
            "head_sha, base_sha, merge_base_sha or diff_sha256"
        )


def applies(flow: Any, execution: Any) -> bool:
    """Whether the checks apply: a read-only review with a CI admission."""
    from preloop.services.read_only_review import read_only_review

    binding = getattr(execution, "ci_review_binding", None)
    return read_only_review(getattr(flow, "git_clone_config", None)) and (
        isinstance(binding, Mapping) and bool(binding.get("head_sha"))
    )


def inspection_required(flow: Any) -> bool:
    """The evidence floor: on unless the flow sets ``require_inspection: false``."""
    block = review_integrity_block(getattr(flow, "git_clone_config", None))
    return block.get("require_inspection") is not False


def _bindings(flow: Any, result: Mapping[str, Any]) -> dict[str, tuple[str, bool]]:
    """Field to (fact, required). Explicit bindings are always required."""
    block = review_integrity_block(getattr(flow, "git_clone_config", None))
    explicit = block.get("result_bindings")
    if isinstance(explicit, Mapping):
        return {
            str(field): (str(fact), True)
            for field, fact in explicit.items()
            if fact in FACTS
        }
    schema = getattr(flow, "result_schema", None)
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    declared = set(properties) if isinstance(properties, Mapping) else set()
    listed = schema.get("required") if isinstance(schema, Mapping) else None
    required = set(listed) if isinstance(listed, list) else set()
    return {
        field: (fact, field in required)
        for field, fact in DEFAULT_BINDINGS.items()
        if field in declared or field in result
    }


def controller_facts(
    binding: Mapping[str, Any] | None, diff: Mapping[str, Any] | None
) -> dict[str, str | None]:
    """Admitted and computed revision identity; unknown facts are None."""
    binding = binding if isinstance(binding, Mapping) else {}
    diff = diff if isinstance(diff, Mapping) else {}

    def sha(value: Any) -> str | None:
        return value if isinstance(value, str) and _SHA_RE.fullmatch(value) else None

    digest = diff.get("sha256")
    return {
        FACT_HEAD: sha(binding.get("head_sha")),
        FACT_BASE: sha(binding.get("base_sha")),
        FACT_MERGE_BASE: sha(diff.get("merge_base_sha")),
        FACT_DIFF: digest
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        else None,
    }


def evaluate(
    flow: Any,
    *,
    result: Any,
    binding: Mapping[str, Any] | None,
    diff: Mapping[str, Any] | None,
    inspection: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Decide whether a review result may stand as a review of this change.

    Args:
        flow: The review flow.
        result: The captured result document.
        binding: The execution's ``ci_review_binding``.
        diff: The controller-computed diff identity, or None.
        inspection: ``inspection_summary`` of the captured tool evidence.

    Returns:
        ``status`` (verified or rejected), ``category`` (None when verified),
        ``reasons``, per-field ``bindings`` without values, and the
        ``inspection`` counts.
    """
    document = result if isinstance(result, Mapping) else {}
    facts = controller_facts(binding, diff)
    reasons: list[str] = []
    checked: list[dict[str, Any]] = []
    for field, (fact, required) in sorted(_bindings(flow, document).items()):
        expected = facts.get(fact)
        claimed = document.get(field)
        if field not in document:
            outcome = "missing" if required else "absent"
        elif expected is None:
            outcome = "fact_unavailable"
        elif not isinstance(claimed, str) or claimed.strip().lower() != expected:
            outcome = "mismatch"
        else:
            outcome = "matched"
        checked.append({"field": field, "fact": fact, "outcome": outcome})
        if outcome in {"missing", "mismatch", "fact_unavailable"}:
            reasons.append(f"{field}_{outcome}")
    counts = dict(inspection) if isinstance(inspection, Mapping) else {}
    inspected = bool(counts.get("repository_tool_calls")) or bool(
        counts.get("native_commands")
    )
    category = None
    if reasons:
        category = CATEGORY_BINDING_MISMATCH
    if inspection_required(flow) and not inspected:
        reasons.append("no_repository_inspection")
        category = category or CATEGORY_NOT_INSPECTED
    return {
        "status": STATUS_REJECTED if category else STATUS_VERIFIED,
        "category": category,
        "reasons": reasons,
        "bindings": checked,
        "inspection": {
            "required": inspection_required(flow),
            "inspected": inspected,
            "repository_tool_calls": int(counts.get("repository_tool_calls") or 0),
            "other_tool_calls": int(counts.get("other_tool_calls") or 0),
            "native_commands": int(counts.get("native_commands") or 0),
        },
    }


def failure_message(outcome: Mapping[str, Any]) -> str:
    """A stable, value-free message for the execution row."""
    reasons = ", ".join(outcome.get("reasons") or []) or "unverified"
    if outcome.get("category") == CATEGORY_NOT_INSPECTED:
        return (
            "review_not_inspected: the review made no repository tool call or "
            f"command, so its verdict is not recorded ({reasons})"
        )
    return (
        "review_binding_mismatch: the result does not name the admitted "
        f"revision ({reasons})"
    )


def count_observations(documents: Any) -> dict[str, int]:
    """Count inspecting tool calls in stored tool evidence documents.

    Gateway documents contribute response-side calls (the model asked for a
    tool); a request-side replay of the same call is not counted twice.
    Native documents contribute command events, counted once per item.
    """
    calls: dict[str, str | None] = {}
    commands: set[str] = set()
    anonymous = 0
    for document in documents or []:
        if not isinstance(document, Mapping):
            continue
        for item in document.get("observations") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("source") == "native" and item.get("kind") == "command":
                identity = item.get("item_id")
                if isinstance(identity, str):
                    commands.add(identity)
                continue
            if item.get("source") != "response" or item.get("kind") != "call":
                continue
            identity = item.get("call_id")
            name = item.get("name") if isinstance(item.get("name"), str) else None
            if isinstance(identity, str):
                calls[identity] = name
            elif name not in NON_INSPECTING_TOOLS:
                anonymous += 1
    repository = anonymous + sum(
        1 for name in calls.values() if name not in NON_INSPECTING_TOOLS
    )
    return {
        "repository_tool_calls": repository,
        "other_tool_calls": len(calls) + anonymous - repository,
        "native_commands": len(commands),
    }


def evaluate_execution(db: Any, flow: Any, execution: Any) -> dict[str, Any]:
    """Evaluate a stored execution from its stored evidence.

    Args:
        db: Database session.
        flow: The execution's flow.
        execution: The execution row.

    Returns:
        The ``evaluate`` outcome, or ``status: not_applicable``.
    """
    if not applies(flow, execution):
        return {"status": STATUS_NOT_APPLICABLE, "category": None, "reasons": []}
    from preloop.models.crud.execution_tool_evidence import inspection_summary
    from preloop.services.review_diff import review_diff_identity

    return evaluate(
        flow,
        result=getattr(execution, "result", None),
        binding=getattr(execution, "ci_review_binding", None),
        diff=review_diff_identity(execution),
        inspection=inspection_summary(db, execution_id=execution.id),
    )
