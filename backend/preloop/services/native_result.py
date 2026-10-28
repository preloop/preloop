"""Cold-start rules for schema-constrained Codex native results.

The pinned Codex CLI's ``exec resume`` has no ``--output-schema`` flag, so a
native-result run can never resume a prior session without losing its schema.
Follow-up and resumed rounds therefore start cold: session restoration inputs
are dropped, a session restored at runtime is not resumed, and the run says
that prior context was not restored.
"""

from typing import Any

RESTORE_KEYS = (
    "cli_session_restore_archive",
    "native_session_reference",
    "confirmation_nudge",
)

COLD_START_MESSAGE = (
    "native_result: this follow-up round starts cold; the prior agent session "
    "context was not restored because resume cannot keep the result schema"
)


def native_result_enabled(config: Any) -> bool:
    """Return whether an agent config opts into native result capture.

    Args:
        config: The flow's ``agent_config``.

    Returns:
        True when ``native_result`` is explicitly enabled.
    """
    return isinstance(config, dict) and config.get("native_result") is True


def native_result_cold_start(flow: Any, event_data: dict[str, Any] | None) -> bool:
    """Return whether a resumed native-result execution must start cold.

    Args:
        flow: Flow being executed.
        event_data: Trigger event data, which carries ``_resume`` for
            correlated follow-ups.

    Returns:
        True when the flow uses native results and the trigger asks to resume.
    """
    if not native_result_enabled(getattr(flow, "agent_config", None)):
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
