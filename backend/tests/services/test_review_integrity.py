"""Result binding and evidence floor for read-only reviews (#2100)."""

from types import SimpleNamespace
from typing import Any

import pytest

from preloop.services import review_integrity
from preloop.services.review_integrity import (
    count_observations,
    evaluate,
    validate_review_integrity_block,
)

HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
DIGEST = "d" * 64
ZERO = "0" * 40
PLACEHOLDER = {
    "review": "Review completed",
    "status": "success",
    "success": True,
    "verdict": "pass",
    "head_sha": ZERO,
    "base_sha": ZERO,
    "findings": [],
}
SCHEMA = {
    "type": "object",
    "required": ["verdict", "head_sha", "base_sha"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "head_sha": {"type": "string"},
        "base_sha": {"type": "string"},
        "merge_base_sha": {"type": "string"},
    },
}
INSPECTED = {
    "diff_commands": 1,
    "repository_tool_calls": 1,
    "other_tool_calls": 0,
    "native_commands": 0,
}
NOTHING = {"repository_tool_calls": 0, "other_tool_calls": 0, "native_commands": 0}


def test_directory_and_revision_queries_do_not_establish_diff_inspection() -> None:
    outcome = run(
        {**PLACEHOLDER, "head_sha": HEAD, "base_sha": BASE},
        inspection={"repository_tool_calls": 2, "native_commands": 2},
    )
    assert outcome["category"] == "review_not_inspected"


def test_degenerate_review_precedes_binding_failure() -> None:
    outcome = run(
        {**PLACEHOLDER, "review": "n", "findings": [{"problem": "n", "fix": "n"}]},
        inspection={"diff_commands": 1},
    )
    assert outcome["category"] == "review_degenerate"


def test_short_no_issues_review_is_usable() -> None:
    outcome = run(
        {
            **PLACEHOLDER,
            "review": "No issues found",
            "head_sha": HEAD,
            "base_sha": BASE,
        },
        inspection={"diff_commands": 1},
    )
    assert outcome["status"] == "verified"


@pytest.mark.parametrize(
    "inspection", [{"repository_tool_calls": 2}, {"diff_commands": 1}]
)
def test_one_word_review_without_findings_is_degenerate(
    inspection: dict[str, int],
) -> None:
    outcome = run(
        {**PLACEHOLDER, "review": "starting", "head_sha": HEAD, "base_sha": BASE},
        inspection=inspection,
    )
    assert outcome["category"] == "review_degenerate"


def flow(**integrity: Any) -> SimpleNamespace:
    config: dict[str, Any] = {"publication_mode": "read_only"}
    if integrity:
        config["review_integrity"] = integrity
    return SimpleNamespace(git_clone_config=config, result_schema=SCHEMA)


def run(
    result: Any,
    *,
    inspection: dict[str, int] | None = None,
    diff: dict[str, Any] | None = None,
    **integrity: Any,
) -> dict[str, Any]:
    return evaluate(
        flow(**integrity),
        result=result,
        binding={"head_sha": HEAD, "base_sha": BASE},
        diff=diff,
        inspection=INSPECTED if inspection is None else inspection,
    )


def test_reported_placeholder_pass_is_rejected_for_both_reasons() -> None:
    outcome = run(PLACEHOLDER, inspection=NOTHING)
    assert outcome["status"] == "rejected"
    # A review that read nothing is reported as not inspected, not as a
    # binding mismatch: the wrong ids follow from not reading the change.
    assert outcome["category"] == "review_not_inspected"
    assert outcome["reasons"] == [
        "base_sha_mismatch",
        "head_sha_mismatch",
        "no_repository_inspection",
    ]
    # Outcomes name fields and facts, never the values compared.
    assert ZERO not in repr(outcome) and HEAD not in repr(outcome)
    assert outcome["inspection"]["inspected"] is False


def test_bound_and_inspected_review_is_verified() -> None:
    outcome = run({**PLACEHOLDER, "head_sha": HEAD, "base_sha": BASE})
    assert outcome["status"] == "verified"
    assert outcome["category"] is None
    # merge_base_sha is declared but optional, so its absence is not a claim.
    assert [b["outcome"] for b in outcome["bindings"]] == [
        "matched",
        "matched",
        "absent",
    ]


def test_correct_ids_without_inspection_fail_the_floor() -> None:
    outcome = run({**PLACEHOLDER, "head_sha": HEAD, "base_sha": BASE}, inspection={})
    assert outcome["category"] == "review_not_inspected"
    assert outcome["reasons"] == ["no_repository_inspection"]


def test_floor_can_be_turned_off_but_binding_still_applies() -> None:
    bound = {**PLACEHOLDER, "head_sha": HEAD, "base_sha": BASE}
    assert run(bound, inspection=NOTHING, require_inspection=False)["status"] == (
        "verified"
    )
    assert (
        run(PLACEHOLDER, inspection=NOTHING, require_inspection=False)["category"]
        == "review_binding_mismatch"
    )


@pytest.mark.parametrize("value", [None, "", HEAD[:7], 40, "f" * 40])
def test_missing_short_or_foreign_head_is_rejected(value: Any) -> None:
    document = {"verdict": "pass", "base_sha": BASE, "head_sha": value}
    if value is None:
        document.pop("head_sha")
    outcome = run(document)
    assert outcome["category"] == "review_binding_mismatch"
    assert outcome["reasons"][0].startswith("head_sha_")


def test_uppercase_admitted_head_matches() -> None:
    document = {"verdict": "pass", "base_sha": BASE, "head_sha": HEAD.upper()}
    assert run(document)["status"] == "verified"


def test_optional_default_field_may_be_omitted_but_not_wrong() -> None:
    document = {"verdict": "pass", "head_sha": HEAD, "base_sha": BASE}
    assert run(document)["status"] == "verified"
    computed = {"merge_base_sha": MERGE_BASE, "sha256": DIGEST}
    wrong = {**document, "merge_base_sha": ZERO}
    assert run(wrong, diff=computed)["reasons"] == ["merge_base_sha_mismatch"]
    assert run(wrong)["reasons"] == ["merge_base_sha_fact_unavailable"]
    right = {**document, "merge_base_sha": MERGE_BASE}
    assert run(right, diff=computed)["status"] == "verified"


def test_explicit_bindings_replace_defaults_and_are_required() -> None:
    bindings = {"reviewed_commit": "head_sha", "diff_digest": "diff_sha256"}
    computed = {"merge_base_sha": MERGE_BASE, "sha256": DIGEST}
    outcome = run(PLACEHOLDER, diff=computed, result_bindings=bindings)
    assert outcome["reasons"] == ["diff_digest_missing", "reviewed_commit_missing"]
    good = {"reviewed_commit": HEAD, "diff_digest": DIGEST, "head_sha": ZERO}
    assert run(good, diff=computed, result_bindings=bindings)["status"] == "verified"
    # An explicit empty map binds nothing; the floor still applies.
    assert run(PLACEHOLDER, result_bindings={})["status"] == "verified"
    assert run(PLACEHOLDER, inspection=NOTHING, result_bindings={})["category"] == (
        "review_not_inspected"
    )


def test_write_only_and_unknown_tools_are_not_inspection() -> None:
    document = {
        "observations": [
            {"source": "response", "kind": "call", "call_id": n, "name": n}
            for n in ("apply_patch", "some_future_tool", "view_image")
        ]
        + [{"source": "response", "kind": "call", "name": "apply_patch"}]
    }
    assert count_observations([document]) == {
        "repository_tool_calls": 0,
        "other_tool_calls": 4,
        "native_commands": 0,
        "diff_commands": 0,
    }


def test_posting_requires_a_matching_head_binding() -> None:
    optional = flow()
    optional.result_schema = {**SCHEMA, "required": ["verdict"]}
    document = {"verdict": "pass", "base_sha": BASE}
    assert (
        evaluate(
            optional,
            result=document,
            binding={"head_sha": HEAD, "base_sha": BASE},
            diff=None,
            inspection=INSPECTED,
        )["status"]
        == "verified"
    )
    outcome = evaluate(
        optional,
        result=document,
        binding={"head_sha": HEAD, "base_sha": BASE},
        diff=None,
        inspection=INSPECTED,
        require_head=True,
    )
    assert outcome["reasons"] == ["head_sha_unbound"]
    assert outcome["category"] == "review_binding_mismatch"
    bound = {**document, "head_sha": HEAD}
    assert (
        evaluate(
            optional,
            result=bound,
            binding={"head_sha": HEAD, "base_sha": BASE},
            diff=None,
            inspection=INSPECTED,
            require_head=True,
        )["status"]
        == "verified"
    )


def test_counts_come_from_captured_evidence_not_claims() -> None:
    gateway = {
        "observations": [
            {
                "source": "response",
                "kind": "call",
                "call_id": "c1",
                "name": "exec_command",
            },
            {
                "source": "request",
                "kind": "call",
                "call_id": "c1",
                "name": "exec_command",
            },
            {"source": "request", "kind": "result", "call_id": "c1"},
            {
                "source": "response",
                "kind": "call",
                "call_id": "c2",
                "name": "request_user_input",
            },
        ]
    }
    assert count_observations([gateway]) == {
        "repository_tool_calls": 1,
        "other_tool_calls": 1,
        "native_commands": 0,
        "diff_commands": 0,
    }
    native = {
        "observations": [
            {"source": "native", "kind": "command", "item_id": "i1"},
            {"source": "native", "kind": "command", "item_id": "i1"},
        ]
    }
    assert count_observations([native])["native_commands"] == 1
    # A request replaying a call the model never made in a response, or a
    # meta tool, is not inspection.
    replay = {"observations": [gateway["observations"][1], gateway["observations"][3]]}
    assert count_observations([replay])["repository_tool_calls"] == 0


@pytest.mark.parametrize(
    "block",
    [
        [],
        {"unknown": True},
        {"require_inspection": "yes"},
        {"result_bindings": {"head_sha": "commit"}},
        {"result_bindings": {"bad-name": "head_sha"}},
        {"result_bindings": ["head_sha"]},
    ],
)
def test_malformed_block_is_rejected(block: Any) -> None:
    with pytest.raises(ValueError):
        validate_review_integrity_block(block)


def test_block_is_validated_on_the_flow_and_only_in_read_only_mode() -> None:
    from pydantic import ValidationError

    from preloop.models.schemas.flow import GitCloneConfig

    from uuid import uuid4

    repository = {"project_id": str(uuid4()), "tracker_id": str(uuid4())}
    base = {"enabled": True, "repositories": [repository]}
    GitCloneConfig(
        **base,
        publication_mode="read_only",
        review_integrity={"result_bindings": {"reviewed": "head_sha"}},
    )
    with pytest.raises(ValidationError):
        GitCloneConfig(
            **base,
            publication_mode="read_only",
            review_integrity={"result_bindings": {"reviewed": "commit"}},
        )
    with pytest.raises(ValidationError):
        GitCloneConfig(**base, review_integrity={"require_inspection": False})


def test_applies_only_to_ci_admitted_read_only_reviews() -> None:
    admitted = SimpleNamespace(ci_review_binding={"head_sha": HEAD})
    assert review_integrity.applies(flow(), admitted)
    assert not review_integrity.applies(SimpleNamespace(git_clone_config={}), admitted)
    assert not review_integrity.applies(flow(), SimpleNamespace(ci_review_binding=None))


def test_startup_receipt_status_is_explicit() -> None:
    from preloop.services.read_only_review import startup_receipt_status

    def row(status: str, binding: Any = None) -> SimpleNamespace:
        return SimpleNamespace(
            status=status,
            ci_review_binding={"head_sha": HEAD} if binding is None else binding,
        )

    verified = {"checkout_verified": True, "setup_completed": True}
    withdrawn = {"checkout_verified": False, "setup_completed": False}
    assert startup_receipt_status(row("RUNNING"), None) == "pending"
    assert startup_receipt_status(row("PENDING"), None) == "pending"
    assert startup_receipt_status(row("RUNNING"), verified) == "recorded"
    assert startup_receipt_status(row("RUNNING"), withdrawn) == "failed"
    assert startup_receipt_status(row("FAILED"), None) == "failed"
    assert startup_receipt_status(row("SUCCEEDED"), None) == "failed"
    for status in ("ABORTED", "CANCELED", "CANCELLED", "TIMED_OUT"):
        assert startup_receipt_status(row(status), None) == "failed"
    assert startup_receipt_status(row("RUNNING", binding=False), None) is None


@pytest.mark.parametrize("category", ["result_schema_invalid", "no_confirmation"])
def test_native_review_without_inspection_reports_the_missing_work(
    category: str,
) -> None:
    from unittest.mock import MagicMock, patch
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = FlowExecutionOrchestrator(MagicMock(), "flow", {}, MagicMock())
    orchestrator.flow = flow()
    orchestrator._native_result_effective = True
    orchestrator.execution_log = SimpleNamespace(
        id="execution",
        ci_review_binding={"head_sha": HEAD, "base_sha": BASE},
        startup_receipt=None,
    )
    orchestrator._record_completion = MagicMock()
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={},
    ):
        status, result, _ = orchestrator._apply_review_integrity_gate(
            "FAILED", {"failure_category": category}, None
        )
    assert status == "FAILED"
    assert result["failure_category"] == "review_not_inspected"


@pytest.mark.parametrize(
    "field,value", [("review", "..."), ("problem", "n"), ("evidence", "-")]
)
def test_each_required_narrative_must_be_substantive(field: str, value: str) -> None:
    review = flow()
    review.result_schema = {
        "type": "object",
        "required": ["review", "findings"],
        "properties": {
            "review": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["problem", "evidence"],
                    "properties": {
                        "problem": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }
    document = {
        "review": "Two defects found",
        "findings": [
            {
                "problem": "Incorrect documented return value",
                "evidence": "The function raises ValueError",
            }
        ],
    }
    if field == "review":
        document[field] = value
    else:
        document["findings"][0][field] = value
    assert review_integrity.degenerate_review(review, document)


@pytest.mark.parametrize(
    "bindings,properties,expected",
    [
        (None, {"head_sha": {}}, "head_sha = " + HEAD),
        (
            {"reviewed_commit": "head_sha"},
            {"reviewed_commit": {}},
            "reviewed_commit = " + HEAD,
        ),
    ],
)
def test_result_prompt_uses_only_schema_fields(
    bindings: Any, properties: dict, expected: str
) -> None:
    review = flow(**({"result_bindings": bindings} if bindings is not None else {}))
    review.result_schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    prompt = review_integrity.result_binding_instruction(
        review, {"head_sha": HEAD, "base_sha": BASE}
    )
    assert expected in prompt
    assert "base_sha =" not in prompt
    assert "additional fields" in prompt


@pytest.mark.parametrize("text,degenerate", [("...", True), ("No issues found", False)])
def test_required_narrative_and_enum_follow_local_schema_references(
    text: str, degenerate: bool
) -> None:
    review = flow()
    review.result_schema = {
        "$ref": "#/$defs/result",
        "$defs": {
            "result": {
                "type": "object",
                "required": ["review", "risk", "evidence"],
                "properties": {
                    "review": {"type": "string"},
                    "risk": {"$ref": "#/$defs/risk"},
                    "evidence": {"type": "string"},
                },
            },
            "risk": {"type": "string", "enum": ["L", "H"]},
        },
    }
    assert (
        review_integrity.degenerate_review(
            review,
            {
                "review": text,
                "risk": "L",
                "evidence": "The changed paths were reviewed",
            },
        )
        is degenerate
    )


@pytest.mark.parametrize(
    "control,value", [("result", "ok"), ("confidence", "ok"), ("priority", "P0")]
)
def test_short_non_enum_controls_are_not_review_narrative(
    control: str, value: str
) -> None:
    review = flow()
    review.result_schema = {
        "type": "object",
        "required": [control, "review"],
        "properties": {control: {"type": "string"}, "review": {"type": "string"}},
    }
    assert not review_integrity.degenerate_review(
        review, {control: value, "review": "No issues found"}
    )
