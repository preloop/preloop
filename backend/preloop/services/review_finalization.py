"""Pure selection and validation shared by live and stored review finalization."""

from dataclasses import dataclass
from typing import Any

from preloop.services import review_integrity
from preloop.services.flow_result_schema import (
    flow_result_schema,
    validate_result_document,
)
from preloop.services.native_result import NATIVE_CANDIDATES_KEY


@dataclass(frozen=True)
class Selection:
    """One selected document and its capture diagnostics."""

    document: Any
    source: str
    error: str | None = None
    unwrapped: str | None = None


def select_native_candidate(
    document: Any,
    *,
    enabled: bool,
    native_valid: bool,
    file_valid: bool,
    native_failure_valid: bool,
) -> Selection:
    """Select once, without trusting a transport key as validation or authority.

    Explicit native failures win; otherwise prefer a verified native answer,
    then a verified file, then retain the native/file for failure diagnostics.
    The validation facts are computed by the caller before this pure decision.
    """
    if not isinstance(document, dict) or NATIVE_CANDIDATES_KEY not in document:
        return Selection(document, "stored_result")
    candidates = document[NATIVE_CANDIDATES_KEY]
    if not enabled or not isinstance(candidates, dict):
        return Selection(None, "unavailable", "selection_unavailable")
    native, file_result = candidates.get("native"), candidates.get("file")
    error = (
        "not_object"
        if native is not None and not isinstance(native, dict)
        else candidates.get("native_error")
    )
    unwrapped = (
        "markdown_fence"
        if native is not None and candidates.get("native_unwrapped") == "markdown_fence"
        else None
    )
    if native_failure_valid or native_valid:
        return Selection(native, "native", error, unwrapped)
    if file_valid:
        return Selection(file_result, "file", error, unwrapped)
    return Selection(
        native if native is not None else file_result,
        "native" if native is not None else "file",
        error,
        unwrapped,
    )


def evaluate_review_document(
    flow: Any,
    document: Any,
    *,
    binding: Any,
    diff: Any,
    inspection: Any,
    require_head: bool = False,
) -> dict[str, Any]:
    """Apply the controller schema, revision, inspection and integrity sequence.

    All inputs are stored facts or live controller observations. Neither result
    metadata nor a previous validation can stand in for a gate.
    """
    schema = flow_result_schema(flow)
    validation = validate_result_document(document, schema) if schema else None
    integrity = review_integrity.evaluate(
        flow,
        result=document,
        binding=binding,
        diff=diff,
        inspection=inspection,
        require_head=require_head,
    )
    return {"validation": validation, "integrity": integrity}
