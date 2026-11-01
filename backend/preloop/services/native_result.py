"""Native Codex structured results: when they are on, and cold-start rules.

``agent_config.native_result`` asks the Codex CLI to constrain its final
answer to the flow's ``result_schema`` and write it to ``result.json``
itself. An explicit ``true`` or ``false`` on the flow always wins. When the
key is absent it defaults to on for Codex review flows (the Codex review
presets, and any flow in read-only review mode) that have an enabled result
schema, because a review that exits 0 without writing its own result is the
common way those runs fail. Without a schema there is nothing to constrain
to, so the default stays off. The effective value is recorded on the
execution under :data:`NATIVE_RESULT_RECORD_KEY`.

The pinned Codex CLI's ``exec resume`` has no ``--output-schema`` flag, so a
native-result run can never resume a prior session without losing its schema.
Follow-up and resumed rounds therefore start cold: session restoration inputs
are dropped, a session restored at runtime is not resumed, and the run says
that prior context was not restored.
"""

from typing import Any, Optional

#: Reserved ``trigger_event_details`` key under which the controller records
#: the native-result setting a run used: ``{"value": bool, "source":
#: "flow" | "default"}``. Never accepted from a trigger body.
NATIVE_RESULT_RECORD_KEY = "_native_result"

#: Codex presets that review code and end with a result document.
CODEX_REVIEW_PRESET_SLUGS: frozenset[str] = frozenset(
    {
        "pull-request-reviewer",
        "architecture-strategy-review",
        "repo-code-health-review",
        "standards-compliance-walk",
        "docs-currency-review",
        "portfolio-review",
    }
)

#: Where the failure message points an operator.
NATIVE_RESULT_DOC = (
    "docs/guide/flows/repo-review-presets.md#native-codex-structured-results"
)

NATIVE_SOURCE_FLOW = "flow"
NATIVE_SOURCE_DEFAULT = "default"

RESTORE_KEYS = (
    "cli_session_restore_archive",
    "native_session_reference",
    "confirmation_nudge",
)

COLD_START_MESSAGE = (
    "native_result: this follow-up round starts cold; the prior agent session "
    "context was not restored because resume cannot keep the result schema"
)


def resolve_native_result(
    flow: Any, *, preset_slug: Optional[str] = None
) -> tuple[bool, str]:
    """The native-result setting a run of ``flow`` uses, and where it came from.

    Args:
        flow: Flow row or namespace (``agent_config``, ``agent_type``,
            ``git_clone_config``, ``result_schema``, ``validate_result``).
        preset_slug: Catalog slug of the preset the flow was cloned from (or
            is), when known.

    Returns:
        ``(enabled, source)``; ``source`` is ``flow`` for an explicit boolean
        on the flow and ``default`` otherwise.
    """
    config = getattr(flow, "agent_config", None)
    explicit = config.get("native_result") if isinstance(config, dict) else None
    if isinstance(explicit, bool):
        return explicit, NATIVE_SOURCE_FLOW
    if str(getattr(flow, "agent_type", "") or "").lower() != "codex":
        return False, NATIVE_SOURCE_DEFAULT
    # Lazy: read_only_review pulls in the CRUD layer, and the agent script
    # builders import this module.
    from preloop.services.read_only_review import read_only_review

    review = read_only_review(getattr(flow, "git_clone_config", None)) or (
        preset_slug in CODEX_REVIEW_PRESET_SLUGS
    )
    schema_enabled = bool(getattr(flow, "result_schema", None)) and (
        getattr(flow, "validate_result", True) is not False
    )
    return bool(review and schema_enabled), NATIVE_SOURCE_DEFAULT


def apply_native_result_default(config: Any, enabled: bool) -> Any:
    """Return ``config`` with the effective native-result value filled in.

    An explicit key is left as is; the flow's own dict is never mutated.

    Args:
        config: The flow's ``agent_config``.
        enabled: The value :func:`resolve_native_result` chose.

    Returns:
        A copy carrying ``native_result`` when it was absent, else ``config``.
    """
    if not isinstance(config, dict):
        return {"native_result": True} if enabled else config
    if "native_result" in config:
        return config
    return {**config, "native_result": enabled}


def native_result_enabled(config: Any) -> bool:
    """Return whether an agent config opts into native result capture.

    Args:
        config: The flow's ``agent_config``.

    Returns:
        True when ``native_result`` is explicitly enabled.
    """
    return isinstance(config, dict) and config.get("native_result") is True


def native_result_cold_start(
    flow: Any,
    event_data: dict[str, Any] | None,
    *,
    enabled: Optional[bool] = None,
) -> bool:
    """Return whether a resumed native-result execution must start cold.

    Args:
        flow: Flow being executed.
        event_data: Trigger event data, which carries ``_resume`` for
            correlated follow-ups.
        enabled: The effective native-result value when the caller already
            resolved it (defaults included); read from the flow otherwise.

    Returns:
        True when the flow uses native results and the trigger asks to resume.
    """
    if enabled is None:
        enabled = native_result_enabled(getattr(flow, "agent_config", None))
    if not enabled:
        return False
    event_data = event_data or {}
    return bool(event_data.get("_resume")) or any(
        event_data.get(key) for key in RESTORE_KEYS
    )


def cold_start_context(context: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Strip session-restore inputs from a native-result execution context.

    Args:
        context: Execution context handed to the agent script builder.

    Returns:
        The context to build from, and whether anything was stripped. The
        caller's dict is never mutated.
    """
    if not native_result_enabled(context.get("agent_config")):
        return context, False
    if not any(context.get(key) for key in RESTORE_KEYS):
        return context, False
    cleaned = {key: value for key, value in context.items() if key not in RESTORE_KEYS}
    return cleaned, True
