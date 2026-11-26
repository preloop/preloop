"""Re-finalize an intact stored review without starting an agent or billing usage."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from preloop.models import crud, models
from preloop.models.crud import execution_refinalize as store
from preloop.models.crud.execution_tool_evidence import inspection_summary
from preloop.services import platform_review as publication
from preloop.services import review_integrity
from preloop.services.execution_tool_evidence import digest
from preloop.services.flow_orchestrator import (
    _result_artifact_confirmation,
    _result_completion_confirmation,
)
from preloop.services.flow_result_schema import (
    flow_result_schema,
    validate_result_document,
)
from preloop.services.native_result import NATIVE_CANDIDATES_KEY
from preloop.services.review_completion_evidence import completion_facts
from preloop.services.review_diff import review_diff_identity
from preloop.services.review_finalization import (
    evaluate_review_document,
    select_native_candidate,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class RefinalizeRefusedError(ValueError):
    """Stored facts do not establish eligibility; no outcome was replaced."""


class RefinalizePreconditionFailedError(ValueError):
    """The preview digest is missing or no longer matches."""


def refinalization_mark(execution: Any) -> dict[str, Any] | None:
    """Read only the controller receipt, never an agent result claim."""
    receipt = getattr(execution, "startup_receipt", None) or {}
    mark = receipt.get("_refinalization")
    return mark if isinstance(mark, dict) else None


def _gate(passed: bool, reason: str | None = None, **details: Any) -> dict[str, Any]:
    return {"passed": passed, "reason": None if passed else reason, **details}


def preview_stored(
    db: Session, flow: models.Flow, execution: models.FlowExecution
) -> dict[str, Any]:
    """Evaluate stored facts without mutating rows or trusting old gate claims."""
    facts = completion_facts(execution)
    original = execution.result
    envelope = isinstance(original, dict) and NATIVE_CANDIDATES_KEY in original
    decision = (execution.trigger_event_details or {}).get("_native_result") or {}
    native = decision.get("value") is True
    schema = flow_result_schema(flow)
    diff = review_diff_identity(execution)
    inspection_available = True
    try:
        inspection = inspection_summary(
            db,
            execution_id=execution.id,
            binding=execution.ci_review_binding,
            diff=diff,
            root=review_integrity.inspection_root(
                flow, execution.ci_review_binding or {}
            ),
        )
    except (SQLAlchemyError, ValueError):
        db.rollback()
        inspection, inspection_available = {}, False

    def evaluate(document: Any) -> dict[str, Any]:
        return evaluate_review_document(
            flow,
            document,
            binding=execution.ci_review_binding,
            diff=diff,
            inspection=inspection,
        )

    def valid(document: Any) -> bool:
        checked = evaluate(document)
        return (checked["validation"] or {}).get("valid") is True and (
            checked["integrity"]["status"] == "verified"
        )

    candidates = original.get(NATIVE_CANDIDATES_KEY) if envelope else None
    candidates = candidates if isinstance(candidates, dict) else {}
    answer = candidates.get("native")
    selected = select_native_candidate(
        original,
        enabled=native,
        native_valid=bool(candidates) and valid(answer),
        file_valid=bool(candidates) and valid(candidates.get("file")),
        native_failure_valid=bool(
            schema
            and isinstance(answer, dict)
            and validate_result_document(answer, schema)["valid"]
            and _result_artifact_confirmation(answer) == "failure"
        ),
    )
    checked = evaluate(selected.document)
    validation, integrity = checked["validation"], checked["integrity"]
    agent = facts.get("agent_finished") or {}
    rejected = (facts.get("review_integrity") or {}).get("status") == "rejected"
    stopped = bool(execution.stop_source or execution.stop_reason)
    allowed_category = execution.failure_category in {
        "model_transient",
        "result_selection_failed",
    } or (
        execution.failure_category in {"execution_error", "orchestration_error"}
        and bool(
            set(facts.get("post_result_failures") or [])
            & {
                "finalization",
                "artifact_storage",
                "log_shipping",
                "completion_event_queueing",
            }
        )
    )
    succeeded_valid = (
        execution.status == "SUCCEEDED"
        and schema
        and (validate_result_document(original, schema)["valid"])
    )
    terminal_ok = (
        execution.status in {"FAILED", "SUCCEEDED"}
        and not succeeded_valid
        and (
            allowed_category
            or (execution.status == "SUCCEEDED" and execution.failure_category is None)
        )
        and not rejected
        and not stopped
        and agent.get("status") not in {"STOPPED", "CANCELLED", "TIMEOUT", "TIMED_OUT"}
        and not facts.get("time_limit_after_result")
    )
    provenance = (
        (
            not envelope
            or (
                set(original) <= {NATIVE_CANDIDATES_KEY, "container_termination"}
                and set(candidates)
                <= {"native", "file", "native_error", "native_unwrapped"}
            )
        )
        and facts.get("capture_state") == "captured"
        and type(agent.get("exit_code")) is int
        and agent["exit_code"] == 0
        and facts.get("result_source") in {"file", "native_structured_result"}
        and (
            not envelope
            or (native and facts.get("result_source") == "native_structured_result")
        )
        and not (
            isinstance(selected.document, dict)
            and NATIVE_CANDIDATES_KEY in selected.document
        )
    )
    evidence_available = True
    try:
        evidence = publication.stored_tool_evidence(db, execution)
    except (SQLAlchemyError, ValueError):
        db.rollback()
        evidence, evidence_available = {}, False
    evidence_ok = publication.refinalization_evidence_satisfied(
        evidence,
        flow.git_clone_config,
    )
    gates = {
        "terminal_outcome": _gate(
            bool(terminal_ok),
            "already_succeeded" if succeeded_valid else "not_control_plane_failure",
        ),
        "flow_revision": _gate(
            execution.flow_revision is not None
            and execution.flow_revision == flow.config_revision,
            "flow_revision_changed_or_unknown",
        ),
        "publication_receipt": _gate(
            not ((flow.git_clone_config or {}).get("review_publication") or {}).get(
                "required"
            ),
            "review_publication_gate_unavailable",
        ),
        "read_only_review": _gate(
            review_integrity.applies(flow, execution), "read_only_review_required"
        ),
        "capture_provenance": _gate(bool(provenance), "capture_provenance_unavailable"),
        "schema": _gate(
            bool(validation and validation["valid"]),
            "result_schema_invalid",
            validation=validation,
        ),
        "binding": _gate(
            bool((execution.ci_review_binding or {}).get("head_sha"))
            and bool((execution.ci_review_binding or {}).get("base_sha"))
            and not any(
                item["outcome"] in {"missing", "mismatch", "fact_unavailable"}
                for item in integrity["bindings"]
            ),
            "review_binding_mismatch",
            bindings=integrity["bindings"],
        ),
        "inspection": _gate(
            inspection_available and bool(inspection.get("diff_commands")),
            "review_not_inspected"
            if inspection_available
            else "inspection_unavailable",
        ),
        "integrity": _gate(
            integrity["status"] == "verified",
            integrity["category"],
            reasons=integrity["reasons"],
            outcome=integrity,
        ),
        "confirmation": _gate(
            _result_completion_confirmation(
                selected.document,
                native_valid=native and bool(validation and validation["valid"]),
            )
            == "success",
            "answer_not_complete",
        ),
        "evidence_settled": _gate(
            execution.end_time is not None
            and not publication.evidence_may_still_arrive(execution),
            "evidence_pending",
        ),
        "tool_evidence": _gate(
            evidence_available and bool(evidence_ok),
            "evidence_incomplete"
            if evidence_available
            else "tool_evidence_unavailable",
            evidence=evidence,
        ),
    }
    response = {
        "execution_id": str(execution.id),
        "outcome": "preview",
        "eligible": all(item["passed"] for item in gates.values()),
        "selected_source": selected.source,
        "selected_result": selected.document,
        "selected_result_sha256": digest(selected.document),
        "previous_result_sha256": digest(original),
        "expected_flow_revision": flow.config_revision,
        "gates": gates,
        "new_status": "SUCCEEDED",
        "new_failure_category": None,
    }
    response["digest"] = digest(
        {
            "version": 1,
            "preview": response,
            "status": execution.status,
            "category": execution.failure_category,
            "receipt": execution.startup_receipt,
            "binding": execution.ci_review_binding,
            "native_decision": decision,
            "flow_revision": execution.flow_revision,
            "schema": flow.result_schema,
            "prompt": flow.prompt_template,
            "agent_config": flow.agent_config,
            "posting": flow.git_clone_config,
        }
    )
    return response


async def refinalize_execution(
    db: Session,
    *,
    actor: models.User,
    execution_id: UUID,
    dry_run: bool = True,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Preview by default; atomically apply only the exact reviewed stored state."""
    store.authorize(db, actor)
    execution = crud.crud_flow_execution.get_without_workspace(
        db, execution_id, account_id=actor.account_id, refresh=True
    )
    if execution is None:
        raise LookupError("Flow execution not found")
    flow = crud.crud_flow.get(
        db,
        execution.flow_id,
        account_id=actor.account_id,
        for_update=not dry_run,
        refresh=True,
    )
    if flow is None:
        raise LookupError("Flow execution not found")
    if not dry_run:
        execution = crud.crud_flow_execution.lock_for_runner_completion(
            db, execution_id=execution_id, account_id=actor.account_id
        )
    if execution is None:
        raise LookupError("Flow execution not found")
    mark = refinalization_mark(execution)
    if mark:
        return {
            "execution_id": str(execution_id),
            "outcome": "already_refinalized",
            "eligible": False,
            "refinalization": mark,
        }
    preview = preview_stored(db, flow, execution)
    if not dry_run and expected_digest != preview["digest"]:
        store.audit_refusal(
            db,
            execution=execution,
            actor=actor,
            result_digest=preview["previous_result_sha256"],
            reason="preview_digest_mismatch",
        )
        raise RefinalizePreconditionFailedError(
            "Preview digest does not match; preview again"
        )
    if not dry_run and not preview["eligible"]:
        store.audit_refusal(
            db,
            execution=execution,
            actor=actor,
            result_digest=preview["previous_result_sha256"],
            reason="gates_failed",
        )
        raise RefinalizeRefusedError(
            ", ".join(
                name for name, gate in preview["gates"].items() if not gate["passed"]
            )
        )
    if dry_run:
        preview["publication"] = await publication.preview_refinalized_publication(
            db, flow, execution, preview
        )
        return preview
    mark = {
        "at": datetime.now(timezone.utc).isoformat(),
        "user_id": str(actor.id),
        "user_name": actor.full_name or actor.username,
        "execution_id": str(execution.id),
        "outcome": "refinalized",
        "previous_status": execution.status,
        "previous_failure_category": execution.failure_category,
        "previous_result_sha256": preview["previous_result_sha256"],
        "new_status": "SUCCEEDED",
        "new_failure_category": None,
        "result_sha256": preview["selected_result_sha256"],
        "flow_revision": flow.config_revision,
        "preview_digest": expected_digest,
    }
    store.record(
        db,
        execution=execution,
        actor=actor,
        document=preview["selected_result"],
        validation=preview["gates"]["schema"]["validation"],
        integrity=preview["gates"]["integrity"]["outcome"],
        mark=mark,
    )
    posted = await publication.publish_platform_review(
        db,
        account_id=actor.account_id,
        execution_id=execution.id,
    )
    return {
        **preview,
        "outcome": "refinalized",
        "refinalization": mark,
        "publication": posted
        or await publication.preview_refinalized_publication(
            db, flow, execution, preview
        ),
    }
