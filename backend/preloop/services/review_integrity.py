"""Controller checks that a read-only review result describes what was reviewed.

Integrity checks run on every restricted-CI read-only review that would otherwise
succeed, and again before Preloop posts anything on the pull request:

- **Result binding.** Result fields that name the reviewed revision must
  equal what the controller admitted (``ci_review_binding``) and computed
  (the startup receipt's ``diff``). A schema-valid result whose ``head_sha``
  is all zeros, missing or another commit is not a review of this change.
- **Evidence floor.** A review must successfully read the bound patch or a changed
  file, rather than just query revisions or list the checkout. "Inspected" is
  decided only from tool evidence Preloop captured itself: gateway-observed
  tool calls and completed native command events, never model claims.

These checks are on by default for read-only reviews. ``git_clone_config.
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
CATEGORY_DEGENERATE = "review_degenerate"

STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"
STATUS_NOT_APPLICABLE = "not_applicable"

#: Tools that can read the repository. An allowlist: a tool not named here
#: (``apply_patch``, ``request_user_input``, a future tool) is not evidence
#: that the change was read. Only qualifying completed commands establish inspection.
INSPECTING_TOOLS: frozenset[str] = frozenset(
    {
        "exec_command",
        "shell",
        "local_shell",
        "container.exec",
        "read_file",
        "list_dir",
        "grep_files",
        "Bash",
        "Read",
        "Grep",
        "Glob",
        "LS",
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
    """Whether the checks apply: a read-only review with a CI or event binding."""
    from preloop.services.read_only_review import read_only_review

    binding = getattr(execution, "ci_review_binding", None)
    return read_only_review(getattr(flow, "git_clone_config", None)) and (
        isinstance(binding, Mapping) and bool(binding.get("head_sha"))
    )


def inspection_required(flow: Any) -> bool:
    """The evidence floor: on unless the flow sets ``require_inspection: false``."""
    block = review_integrity_block(getattr(flow, "git_clone_config", None))
    return block.get("require_inspection") is not False


def inspection_root(flow: Any, binding: Mapping[str, Any]) -> str:
    """Resolve the admitted repository's hosted path, including shared flows."""
    config = getattr(flow, "git_clone_config", None) or {}
    repositories = config.get("repositories") or []
    for repository in repositories:
        if str(repository.get("project_id")) == str(binding.get("project_id")):
            path = repository.get("clone_path", "/workspace-1")
            return path if path.startswith("/") else "/workspace/" + path
    return ""


def _schema_view(node: Any, root: Any) -> Mapping[str, Any]:
    """Follow local schema references for narrative and binding declarations.

    Result validation remains the schema validator's responsibility. This
    view only reads authored declarations and never resolves remote resources.
    """
    seen: set[str] = set()
    while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
        reference = node["$ref"]
        if reference in seen or not (reference == "#" or reference.startswith("#/")):
            break
        seen.add(reference)
        target = root
        try:
            for part in reference[2:].split("/") if reference.startswith("#/") else []:
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
        except (KeyError, IndexError, TypeError, ValueError):
            break
        if not isinstance(target, Mapping):
            break
        sibling = {key: value for key, value in node.items() if key != "$ref"}
        node = {**target, **sibling}
        if "properties" in target or "properties" in sibling:
            node["properties"] = {
                **target.get("properties", {}),
                **sibling.get("properties", {}),
            }
        if "required" in target or "required" in sibling:
            node["required"] = list(
                set(target.get("required", [])) | set(sibling.get("required", []))
            )
    return node if isinstance(node, Mapping) else {}


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
    schema = _schema_view(schema, schema)
    properties = schema.get("properties")
    declared = set(properties) if isinstance(properties, Mapping) else set()
    listed = schema.get("required") if isinstance(schema, Mapping) else None
    required = set(listed) if isinstance(listed, list) else set()
    return {
        field: (fact, field in required)
        for field, fact in DEFAULT_BINDINGS.items()
        if field in declared or field in result
    }


def result_binding_instruction(flow: Any, binding: Mapping[str, Any]) -> str:
    """Describe declared result bindings without adding fields to strict schemas."""
    facts = controller_facts(binding, None)
    mappings = [
        f"{field} = {facts[fact]}"
        for field, (fact, _) in _bindings(flow, {}).items()
        if facts.get(fact)
    ]
    return (
        "Result bindings: " + "; ".join(mappings) + ". " if mappings else ""
    ) + "Follow the required result schema; do not add additional fields. "


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


def degenerate_review(flow: Any, document: Mapping[str, Any]) -> bool:
    """Reject tiny narratives and single-word conclusions without findings.

    Identity fields, control fields, schema enums/constants and platform metadata
    are not narrative. Schemas with no narrative fields remain supported.
    """
    excluded = (
        set(_bindings(flow, document))
        | set(DEFAULT_BINDINGS)
        | {
            "status",
            "verdict",
            "severity",
            "priority",
            "confidence",
            "result",
            "success",
            "container_termination",
        }
    )
    strings: list[str] = []
    required_strings: list[str] = []

    root_schema = getattr(flow, "result_schema", None)

    def visit(value: Any, schema: Any, required: bool = False) -> None:
        schema = _schema_view(schema, root_schema)
        if "enum" in schema or "const" in schema:
            return
        if isinstance(value, str):
            strings.append(value.strip())
            if required:
                required_strings.append(value.strip())
        elif isinstance(value, Mapping):
            properties = schema.get("properties") or {}
            for key, child in value.items():
                if key not in excluded and not key.startswith("_"):
                    visit(
                        child,
                        properties.get(key, {}),
                        key in schema.get("required", []),
                    )
        elif isinstance(value, list):
            for child in value:
                visit(child, schema.get("items"))

    visit(document, getattr(flow, "result_schema", None))
    return any(
        sum(char.isalnum() for char in value) < 3 for value in required_strings
    ) or (
        bool(strings)
        and (
            all(sum(char.isalnum() for char in value) < 3 for value in strings)
            or (
                not document.get("findings")
                and all(len(value.split()) < 2 for value in strings)
            )
        )
    )


def evaluate(
    flow: Any,
    *,
    result: Any,
    binding: Mapping[str, Any] | None,
    diff: Mapping[str, Any] | None,
    inspection: Mapping[str, Any] | None,
    require_head: bool = False,
) -> dict[str, Any]:
    """Decide whether a review result may stand as a review of this change.

    Args:
        flow: The review flow.
        result: The captured result document.
        binding: The execution's ``ci_review_binding``.
        diff: The controller-computed diff identity, or None.
        inspection: ``inspection_summary`` of the captured tool evidence.
        require_head: Require a result field bound to ``head_sha`` that
            matches. Platform posting sets this: a posted verdict must name
            the commit it reviewed.

    Returns:
        ``status`` (verified or rejected), ``category`` (None when verified),
        ``reasons``, per-field ``bindings`` without values, and the
        ``inspection`` counts.
    """
    document = result if isinstance(result, Mapping) else {}
    facts = controller_facts(binding, diff)
    reasons: list[str] = []
    checked: list[dict[str, Any]] = []
    degenerate = degenerate_review(flow, document)
    bindings = {} if degenerate else _bindings(flow, document)
    for field, (fact, required) in sorted(bindings.items()):
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
    if require_head and not any(
        item["fact"] == FACT_HEAD and item["outcome"] == "matched" for item in checked
    ):
        reasons.append("head_sha_unbound")
    counts = dict(inspection) if isinstance(inspection, Mapping) else {}
    inspected = bool(counts.get("diff_commands"))
    category = None
    if reasons:
        category = CATEGORY_BINDING_MISMATCH
    if inspection_required(flow) and not inspected:
        # Not reading the change is the cause; ids that do not match the
        # admitted revision follow from it, so this category wins.
        reasons.append("no_repository_inspection")
        category = CATEGORY_NOT_INSPECTED
    if degenerate:
        reasons.insert(0, "narrative_not_substantive")
        category = CATEGORY_DEGENERATE
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
            "diff_commands": int(counts.get("diff_commands") or 0),
        },
    }


def failure_message(outcome: Mapping[str, Any]) -> str:
    """A stable, value-free message for the execution row."""
    reasons = ", ".join(outcome.get("reasons") or []) or "unverified"
    if outcome.get("category") == CATEGORY_DEGENERATE:
        return "review_degenerate: a required narrative is too short or the review contains only a word without findings"
    if outcome.get("category") == CATEGORY_NOT_INSPECTED:
        return (
            "review_not_inspected: no successful command read the bound diff or "
            f"a changed file, so its verdict is not recorded ({reasons})"
        )
    return (
        "review_binding_mismatch: the result does not name the admitted "
        f"revision ({reasons})"
    )


class ObservationCounter:
    """Incremental inspection counts over stored tool evidence documents.

    Gateway documents contribute response-side calls (the model asked for a
    tool); a request-side replay of the same call is not counted. Native
    documents contribute command events, counted once per item.
    """

    def __init__(
        self,
        *,
        binding: Mapping[str, Any] | None = None,
        diff: Mapping[str, Any] | None = None,
        root: str = "",
    ) -> None:
        self._binding = binding or {}
        self._diff = diff or {}
        self._root = root
        self._diff_commands: set[str] = set()
        self._calls: dict[str, str | None] = {}
        self._commands: set[str] = set()
        self._anonymous_inspecting = 0
        self._anonymous_other = 0

    def add(self, document: Any) -> None:
        """Count one stored evidence document."""
        if not isinstance(document, Mapping):
            return
        for item in document.get("observations") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("source") == "native" and item.get("kind") == "command":
                identity = item.get("item_id")
                if isinstance(identity, str):
                    self._commands.add(identity)
                    from preloop.services.review_inspection import (
                        command_reads_change,
                        inspection_receipt,
                    )

                    if (
                        item.get("event") == "item.completed"
                        and type(item.get("exit_code")) is int
                        and item["exit_code"] == 0
                        and (
                            (
                                not item.get("truncated")
                                and not item.get("redacted")
                                and command_reads_change(
                                    item.get("value"),
                                    base=self._binding.get("base_sha", ""),
                                    head=self._binding.get("head_sha", ""),
                                    files=self._diff.get("files", []),
                                    root=self._root,
                                    merge_base=self._diff.get("merge_base_sha") or "",
                                )
                            )
                            or (
                                bool(self._binding.get("base_sha"))
                                and bool(self._binding.get("head_sha"))
                                and item.get("inspection_receipt")
                                == inspection_receipt(
                                    self._binding, self._diff, self._root
                                )
                            )
                        )
                    ):
                        self._diff_commands.add(identity)
                continue
            if item.get("source") != "response" or item.get("kind") != "call":
                continue
            identity = item.get("call_id")
            name = item.get("name") if isinstance(item.get("name"), str) else None
            if isinstance(identity, str):
                self._calls[identity] = name
            elif name in INSPECTING_TOOLS:
                self._anonymous_inspecting += 1
            else:
                self._anonymous_other += 1

    def counts(self) -> dict[str, int]:
        """``repository_tool_calls``, ``other_tool_calls``, ``native_commands``."""
        inspecting = sum(1 for n in self._calls.values() if n in INSPECTING_TOOLS)
        return {
            "repository_tool_calls": inspecting + self._anonymous_inspecting,
            "other_tool_calls": len(self._calls) - inspecting + self._anonymous_other,
            "native_commands": len(self._commands),
            "diff_commands": len(self._diff_commands),
        }

    def inspected(self) -> bool:
        """Whether a successful read of the bound change was seen."""
        counts = self.counts()
        return bool(counts["diff_commands"])


def count_observations(documents: Any) -> dict[str, int]:
    """Count inspecting tool calls in stored tool evidence documents."""
    counter = ObservationCounter()
    for document in documents or []:
        counter.add(document)
    return counter.counts()


def evaluate_execution(
    db: Any, flow: Any, execution: Any, *, require_head: bool = False
) -> dict[str, Any]:
    """Evaluate a stored execution from its stored evidence.

    Args:
        db: Database session.
        flow: The execution's flow.
        execution: The execution row.
        require_head: Passed to :func:`evaluate`.

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
        inspection=inspection_summary(
            db,
            execution_id=execution.id,
            binding=execution.ci_review_binding,
            diff=review_diff_identity(execution),
            root=inspection_root(flow, execution.ci_review_binding),
        ),
        require_head=require_head,
    )
