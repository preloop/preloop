"""In-place completion nudge: one reminder round inside the agent container.

Staging evidence (2026-07-19 .. 2026-09-02, 100 failed flow executions) put
``no_confirmation`` at the top of the table with 33 rows: runs that exited 0
after a median of 0.8 minutes and simply never used a completion channel.
The work may well have been done; what was missing was the confirmation.

The cheapest sound way to ask for it is to ask the agent that is still
sitting there: the container has just run the harness, the workspace is
intact, and every harness Preloop drives non-interactively can be re-entered
on its previous session (``opencode run --continue``, ``codex exec resume
--last``). So the agent script itself, after the harness exits, checks the
completion contract and, when nothing confirmed it, re-invokes the SAME
harness in the SAME container and workspace with a short reminder prompt.
The reminder's own output is judged by exactly the same contract: it goes to
the same stdout the orchestrator streams, and it writes the same
``/workspace/result.json`` the orchestrator captures.

Safety boundary. The block is emitted BEFORE the container's post-execution
git block (push, PR/MR creation) and only runs when the harness exited 0, so
a nudge can never re-run a push. It runs at most once per container, is
bounded by a wall clock, and its exit code is discarded: a failed nudge
leaves the run exactly where it was (fail closed), it never turns a
successful run into a failed one.

Markers on stdout make the round visible to the orchestrator, which turns
them into the ``completion_nudge`` timeline event:

    PRELOOP_COMPLETION_NUDGE                 the reminder round starts
    PRELOOP_COMPLETION_NUDGE_RESULT exit=<n> the reminder round finished
    PRELOOP_COMPLETION_NUDGE_UNSUPPORTED <r> the installed CLI cannot resume

Runtimes that cannot resume a session at all never emit the block; for them
the orchestrator widens the accepted completion signals instead (see
``FlowExecutionOrchestrator._resolve_missing_confirmation``).
"""

from __future__ import annotations

import base64
from typing import Any, Dict

from preloop.config import settings

# Success sentinel that agents print when completing successfully.
# Must match FLOW_SUCCESS_SENTINEL in flow_orchestrator.py.
FLOW_SUCCESS_SENTINEL = "FLOW_EXECUTION_SUCCESS"

# Line-start prefix for an explicit "the task did not complete" report.
# Must match FLOW_FAILURE_REPORT_PREFIX in flow_orchestrator.py.
FLOW_FAILURE_REPORT_PREFIX = "FLOW_EXECUTION_FAILED:"

# Printed on its own line when the reminder round starts. The orchestrator
# matches this EXACTLY (the result marker below shares its prefix).
COMPLETION_NUDGE_MARKER = "PRELOOP_COMPLETION_NUDGE"

# Printed after the reminder round, followed by " exit=<code>".
COMPLETION_NUDGE_RESULT_MARKER = "PRELOOP_COMPLETION_NUDGE_RESULT"

# Printed instead of the round when the installed CLI has no resume flag,
# followed by " <agent label>".
COMPLETION_NUDGE_UNSUPPORTED_MARKER = "PRELOOP_COMPLETION_NUDGE_UNSUPPORTED"

# Where the agent script tees the harness output so the in-container check
# can look for the sentinel on a line of its own, exactly as the
# orchestrator's detector does.
AGENT_OUTPUT_LOG_PATH = "/tmp/preloop-agent-output.log"

# Where the reminder prompt is written (base64-decoded from the script, so
# no prompt text is ever interpreted by the shell).
NUDGE_PROMPT_PATH = "/tmp/preloop-completion-nudge-prompt.txt"

# Structured result report; a top-level "status" or "verdict" string in it is
# a completion signal, so its presence suppresses the nudge.
RESULT_ARTIFACT_PATH = "/workspace/result.json"

# The reminder itself. Deliberately short, and explicit that this is not an
# invitation to keep working: the harness is resumed on the session that just
# ran, so anything it does here is on top of work that may already have had
# external effects.
COMPLETION_NUDGE_PROMPT = f"""You stopped without the completion contract.

Do NOT redo, continue, or extend the task, and do not take any new side effects (no pushes, comments, or API writes). This round exists only to record how the task ended.

If the task ran to completion: write {RESULT_ARTIFACT_PATH} with the completion status or verdict your original instructions require (for example {{"status": "success"}}), preserving any richer report already in the file, and print this marker on a line by itself, with no other text on that line: {FLOW_SUCCESS_SENTINEL}

If it did not run to completion: write {RESULT_ARTIFACT_PATH} as {{"status": "failure", "reason": "<one short sentence>"}} or print a single line that starts with {FLOW_FAILURE_REPORT_PREFIX} followed by the reason.

Then stop."""


def completion_nudge_enabled(execution_context: Dict[str, Any]) -> bool:
    """Whether this session should carry the in-place nudge block.

    Off for the orchestrator's own confirmation round (a nudge inside a nudge
    is double work with nothing to gain) and killable fleet-wide through
    ``FLOW_COMPLETION_NUDGE_ENABLED``. A per-execution
    ``completion_nudge_enabled`` key wins over both, which is what the tests
    and any future per-flow switch use.

    Args:
        execution_context: The execution context the session starts from.

    Returns:
        True when the agent script should embed the reminder round.
    """
    override = execution_context.get("completion_nudge_enabled")
    if isinstance(override, bool):
        return override
    if execution_context.get("confirmation_nudge"):
        return False
    return bool(settings.flow_completion_nudge_enabled)


def completion_nudge_timeout_seconds() -> int:
    """Wall clock for the reminder round, at least 30 seconds."""
    return max(30, int(settings.flow_completion_nudge_timeout_seconds))


def build_completion_nudge_block(
    *,
    agent_label: str,
    exit_code_var: str,
    resume_probe: str,
    resume_command: str,
    timeout_seconds: int,
    output_log_path: str = AGENT_OUTPUT_LOG_PATH,
    result_path: str = RESULT_ARTIFACT_PATH,
    prompt_path: str = NUDGE_PROMPT_PATH,
) -> str:
    """Render the shell block that runs one in-place reminder round.

    The block is inserted after the harness invocation and BEFORE the
    post-execution git block, so it cannot re-run a push. It is a no-op
    unless the harness exited 0 and no completion signal exists yet.

    Args:
        agent_label: Harness name for the operator-facing marker lines.
        exit_code_var: Shell variable holding the harness exit code
            (without ``$``), e.g. ``OPENCODE_EXIT_CODE``.
        resume_probe: Shell condition that succeeds when the installed CLI
            can resume the session it just ran.
        resume_command: Shell command that re-invokes the harness on the
            previous session with ``"$(cat {NUDGE_PROMPT_PATH})"`` as its
            prompt. It may use ``$PRELOOP_NUDGE_TIMEOUT`` as a command
            prefix; the block sets it to ``timeout <n>`` when coreutils
            ``timeout`` exists and to the empty string otherwise.
        timeout_seconds: Wall clock for the round.
        output_log_path: Where the harness output was tee'd (overridable so
            the shell block can be exercised end to end in tests).
        result_path: The structured result report to check for a completion
            status or verdict.
        prompt_path: Where the reminder prompt is written in the container.

    Returns:
        A bash block, safe to embed verbatim in an agent script.
    """
    prompt_b64 = base64.b64encode(COMPLETION_NUDGE_PROMPT.encode()).decode()
    return f"""
# ============================================================
# Preloop completion nudge (at most one round, same container,
# same workspace, same harness session). Runs only after a
# clean exit and only when nothing confirmed completion yet,
# and always BEFORE the post-execution git block so it can
# never re-run a push.
# ============================================================
_preloop_completion_signal() {{
    if [ -s "{output_log_path}" ] \\
        && grep -qxF '{FLOW_SUCCESS_SENTINEL}' "{output_log_path}"; then
        return 0
    fi
    if [ ! -s "{result_path}" ]; then
        return 1
    fi
    # A result.json that parses and carries a top-level status or verdict is
    # a completion signal whatever its value: the orchestrator judges the
    # value, and asking again would only invite the agent to overwrite it.
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(d, dict) and (isinstance(d.get("status"), str) or isinstance(d.get("verdict"), str)) else 1)' "{result_path}"
        return $?
    fi
    if command -v node >/dev/null 2>&1; then
        node -e 'const fs=require("fs");let d;try{{d=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));}}catch(e){{process.exit(1);}}process.exit(d&&typeof d==="object"&&!Array.isArray(d)&&(typeof d.status==="string"||typeof d.verdict==="string")?0:1);' "{result_path}"
        return $?
    fi
    # No JSON parser in the image: a non-empty report counts, because
    # nudging an agent that already wrote one risks losing it.
    return 0
}}

if [ "${{{exit_code_var}}}" -eq 0 ] && ! _preloop_completion_signal; then
    if {resume_probe}; then
        echo '{prompt_b64}' | base64 -d > "{prompt_path}"
        PRELOOP_NUDGE_TIMEOUT=""
        if command -v timeout >/dev/null 2>&1; then
            PRELOOP_NUDGE_TIMEOUT="timeout {timeout_seconds}"
        fi
        echo "{COMPLETION_NUDGE_MARKER}"
        set +e
        {resume_command}
        _PRELOOP_NUDGE_RC=$?
        set -e
        echo "{COMPLETION_NUDGE_RESULT_MARKER} exit=$_PRELOOP_NUDGE_RC"
    else
        echo "{COMPLETION_NUDGE_UNSUPPORTED_MARKER} {agent_label}"
    fi
fi
"""
