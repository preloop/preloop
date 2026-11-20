"""A review that says it did not finish is kept, and is never a pass.

A bound reviewer is told to say so in its answer when it runs out of time,
using the failure or incomplete form of the flow's result schema. These
tests pin what happens to such an answer: the document is stored on the
execution, the run is not a verified review, and the publication plan has
no approving review and no passing status for it. They drive the real
finalize gates (result schema, review integrity) and the real publication
planner; no database or model provider is used.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from preloop.agents.base import AgentStatus
from preloop.services import review_integrity
from preloop.services.platform_review import plan_publication
from tests.services.test_native_result_finalization import (
    BASE,
    HEAD,
    executor,
    expired,
    finalize,
    orchestrator,
)

TEXT = {"type": "string", "minLength": 1}
SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}

#: A result schema whose owner gave it both a failure and an incomplete form.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["base_sha", "head_sha", "review", "status", "verdict", "findings"],
    "properties": {
        "base_sha": SHA,
        "head_sha": SHA,
        "review": TEXT,
        "status": {
            "type": "string",
            "enum": ["success", "failure", "incomplete", "partial"],
        },
        "verdict": {
            "type": "string",
            "enum": ["pass", "changes_requested", "error", "incomplete"],
        },
        "findings": {"type": "array", "items": {"type": "object"}},
        "not_reviewed": {"type": "array", "items": TEXT},
    },
}

#: Every verdict maps to a state, and ``pass`` to an approving review.
PLATFORM_REVIEW = {
    "enabled": True,
    "verdict_actions": {"pass": "approve", "changes_requested": "request_changes"},
    "status": {
        "context": "preloop/review",
        "verdict_states": {
            "pass": "success",
            "changes_requested": "failure",
            "error": "error",
            "incomplete": "success",
        },
        "incomplete_state": "error",
    },
}

UNFINISHED = "Ran out of time after reading parser.py; cli.py was not reviewed."


def document(**overrides: Any) -> dict[str, Any]:
    """A schema-valid, bound, substantive answer that did not finish."""
    value: dict[str, Any] = {
        "base_sha": BASE,
        "head_sha": HEAD,
        "review": UNFINISHED,
        "status": "incomplete",
        "verdict": "pass",
        "findings": [{"problem": "Unchecked length", "evidence": "parser.py:12"}],
        "not_reviewed": ["cli.py"],
    }
    value.update(overrides)
    return value


DECLARED = [
    document(status="failure", verdict="error"),
    document(status="incomplete", verdict="pass"),
    document(status="partial", verdict="pass"),
    document(status="success", verdict="incomplete"),
    document(status="incomplete", verdict="incomplete", findings=[]),
]
IDS = ["failure", "incomplete-status", "partial-status", "incomplete-verdict", "none"]


def review_orchestrator() -> Any:
    """The finalization orchestrator with this module's schema and posting."""
    orch = orchestrator()
    orch.flow.result_schema = SCHEMA
    orch.flow.git_clone_config = {
        "publication_mode": "read_only",
        "platform_review": PLATFORM_REVIEW,
    }
    return orch


def publication(orch: Any, status: str, result: dict[str, Any]) -> Any:
    """Plan publication from the stored outcome, as posting does."""
    stored = result.get("result")
    execution = SimpleNamespace(
        status=status,
        result=stored,
        result_schema_validation=orch._result_schema_validation,
        failure_category=result.get("failure_category"),
    )
    integrity = review_integrity.evaluate(
        orch.flow,
        result=stored,
        binding={"head_sha": HEAD, "base_sha": BASE},
        diff=None,
        inspection={"diff_commands": 1},
        require_head=True,
    )
    return plan_publication(orch.flow, execution, integrity=integrity), integrity


def _kept(stored: Any, answer: dict[str, Any]) -> None:
    """The stored document is the answer, plus at most the integrity note."""
    assert isinstance(stored, dict)
    assert {k: v for k, v in stored.items() if k != "_review_integrity"} == answer


@pytest.mark.parametrize("answer", DECLARED, ids=IDS)
@pytest.mark.asyncio
async def test_declared_unfinished_review_is_kept_and_not_a_pass(
    answer: dict[str, Any],
) -> None:
    orch = review_orchestrator()
    status, result = await finalize(orch, executor(answer, AgentStatus.SUCCEEDED))

    assert orch._result_schema_validation["valid"] is True
    _kept(result.get("result"), answer)
    assert status == "FAILED", "an unfinished review must not succeed"
    plan, integrity = publication(orch, status, result)
    assert integrity["status"] != review_integrity.STATUS_VERIFIED
    assert plan.review_action is None
    assert plan.status_value != "success"


@pytest.mark.parametrize("answer", DECLARED, ids=IDS)
@pytest.mark.asyncio
async def test_declared_unfinished_review_at_the_time_limit_is_not_kept_as_ready(
    answer: dict[str, Any],
) -> None:
    orch = review_orchestrator()
    expired(orch)
    status, result = await finalize(orch, executor(answer, AgentStatus.RUNNING))

    assert status == "FAILED"
    milestones = [c.args[0] for c in orch.execution_logger.log_milestone.mock_calls]
    assert "native_result_ready_at_time_limit" not in milestones
    plan, _ = publication(orch, status, result)
    assert plan.review_action is None
    assert plan.status_value != "success"


@pytest.mark.asyncio
async def test_finished_review_in_the_same_schema_still_verifies() -> None:
    orch = review_orchestrator()
    answer = document(status="success", verdict="pass", review="Read all files.")
    answer.pop("not_reviewed")
    status, result = await finalize(orch, executor(answer, AgentStatus.SUCCEEDED))

    assert status == "SUCCEEDED", result.get("error_message")
    plan, integrity = publication(orch, status, result)
    assert integrity["status"] == review_integrity.STATUS_VERIFIED
    assert plan.review_action == "approve"
    assert plan.status_value == "success"


@pytest.mark.parametrize(
    "answer",
    [document(status="incomplete"), document(verdict="incomplete", status="success")],
    ids=["status", "verdict"],
)
def test_integrity_names_a_declared_unfinished_review(answer: dict[str, Any]) -> None:
    outcome = review_integrity.evaluate(
        SimpleNamespace(
            result_schema=SCHEMA, git_clone_config={"publication_mode": "read_only"}
        ),
        result=answer,
        binding={"head_sha": HEAD, "base_sha": BASE},
        diff=None,
        inspection={"diff_commands": 1},
    )
    assert outcome["status"] == review_integrity.STATUS_REJECTED
    assert outcome["reasons"][0] == "review_declared_incomplete"


@pytest.mark.asyncio
async def test_declared_unfinished_review_says_why_it_failed() -> None:
    orch = review_orchestrator()
    answer = document(status="incomplete")
    status, result = await finalize(orch, executor(answer, AgentStatus.SUCCEEDED))
    assert status == "FAILED"
    assert result["failure_category"] == review_integrity.CATEGORY_DEGENERATE
    assert "did not finish" in result["error_message"]
