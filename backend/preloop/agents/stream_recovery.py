"""Bounded in-container recovery of a failed CLI's existing conversation.

A continuation prompt is appended to the captured native conversation. The
original task prompt and container initialization/publication blocks never
run again. Provider-level retries remain owned by the CLI and gateway.
"""

from __future__ import annotations

import base64
import shlex

ATTEMPT_LOG_PATH = "/tmp/preloop-agent-attempt.log"
RECOVERY_PROMPT_PATH = "/tmp/preloop-stream-recovery-prompt.txt"
RECOVERY_PROMPT = """The upstream model connection was interrupted. Continue this existing session from the last incomplete turn. Keep completed work and tool results. Do not restart the task or repeat completed tool calls, pushes, comments, or other external writes. If an external action's result is uncertain, inspect its current state before deciding whether anything remains to do. Finish the original task and its completion report."""


RESULT_BASELINE_PATH = "/tmp/preloop-result-before-invocation.json"
_RESULT_FINGERPRINT_SOURCE = """
def result_fingerprint(path):
    try:
        with path.open('rb') as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
            stat = path.stat()
        return [digest.hexdigest(), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
    except FileNotFoundError:
        return None
    except OSError:
        return 'unreadable'
"""


def build_stream_recovery_baseline_block(
    *,
    result_path: str = "/workspace/result.json",
    baseline_path: str = RESULT_BASELINE_PATH,
) -> str:
    """Capture restored report identity before the original CLI invocation.

    Content and stat identity distinguish old completion evidence from a report
    written by this invocation, including a rewrite with identical content.
    """
    return f"""
# Preserve restored result.json; only current invocation reports stop recovery.
python3 - {shlex.quote(result_path)} {shlex.quote(baseline_path)} <<'PRELOOP_RESULT_BASELINE_PY'
import hashlib, json, pathlib, sys
{_RESULT_FINGERPRINT_SOURCE}
pathlib.Path(sys.argv[2]).write_text(json.dumps(result_fingerprint(pathlib.Path(sys.argv[1]))))
PRELOOP_RESULT_BASELINE_PY
"""


def build_stream_recovery_block(
    *,
    agent_label: str,
    exit_code_var: str,
    session_id_expr: str,
    resume_probe: str,
    resume_command: str,
    attempt_log_path: str = ATTEMPT_LOG_PATH,
    prompt_path: str = RECOVERY_PROMPT_PATH,
    result_path: str = "/workspace/result.json",
    baseline_path: str = RESULT_BASELINE_PATH,
    max_resumes: int = 2,
    timeout_seconds: int = 600,
    backoff_seconds: int = 2,
    kill_after_seconds: int = 5,
) -> str:
    """Render bounded explicit-session resumes before container publication.

    ``resume_command`` must update ``exit_code_var`` with the CLI's status,
    preserving pipeline exit codes, and write only that attempt's log. The
    caller supplies code, never external text, for command/variable arguments.
    """
    encoded = base64.b64encode(RECOVERY_PROMPT.encode()).decode()
    return f"""
# Recover only this captured conversation, before the publication block.
_preloop_transient_stream_failure() {{
    # Inspect only the latest CLI invocation, not earlier recovered failures.
    # Explicit terminal faults take precedence over historical network noise.
    python3 - {shlex.quote(attempt_log_path)} {shlex.quote(result_path)} {shlex.quote(baseline_path)} <<'PRELOOP_RECOVERY_PY'
import hashlib, json, pathlib, re, sys
{_RESULT_FINGERPRINT_SOURCE}
try:
    baseline = json.loads(pathlib.Path(sys.argv[3]).read_text())
except (OSError, ValueError):
    baseline = 'unknown'
try:
    report_path = pathlib.Path(sys.argv[2])
    current_fingerprint = result_fingerprint(report_path)
    result = json.loads(report_path.read_text()) if current_fingerprint != baseline else None
    # Same terminal vocabulary as flow_orchestrator._result_artifact_confirmation.
    if isinstance(result, dict):
        status = str(result.get('status') or '').strip().lower()
        verdict = str(result.get('verdict') or '').strip().lower()
        if status in ('success', 'succeeded', 'pass', 'passed', 'fail', 'failure', 'failed', 'error') or verdict in ('pass', 'passed', 'pass_with_findings', 'fail', 'error'):
            sys.exit(1)
except (OSError, ValueError):
    pass
try:
    with pathlib.Path(sys.argv[1]).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        text = stream.read().decode('utf-8', errors='replace').lower()
except OSError:
    sys.exit(1)
terminal = r'(?m)^flow_execution_success$|flow_execution_failed:|verification_denied|invalid.api.key|authentication.error|unauthorized|permission.denied|insufficient_quota|quota.exhausted|billing.hard.limit|context.length.exceeded|run terminated by policy'
transient = r'upstream_disconnect|disconnected (?:mid.stream|before completion)|incomplete chunked read|peer closed connection|connection (?:reset|closed|refused)|econnreset|econnrefused|socket hang up|other side closed|typeerror: terminated|fetch failed|provider_unavailable|midstreamfallbackerror|(?:request|read|socket|operation) timed? out|(?:status(?:_code)?|http)\\W{{0,4}}(?:429|500|502|503|504)\\b'
sys.exit(0 if not re.search(terminal, text) and re.search(transient, text) else 1)
PRELOOP_RECOVERY_PY
}}
_pl_recovery_attempt=0
while [ "${{{exit_code_var}}}" -ne 0 ] && [ "$_pl_recovery_attempt" -lt {max_resumes} ]; do
    case "${{{exit_code_var}}}" in 130|137|143) break ;; esac
    if ! _preloop_transient_stream_failure; then
        break
    fi
    _pl_recovery_sid={session_id_expr}
    if [ -z "$_pl_recovery_sid" ] || ! command -v timeout >/dev/null 2>&1 || ! ( {resume_probe}; ); then
        echo "PRELOOP_STREAM_RECOVERY_UNAVAILABLE {agent_label}"
        break
    fi
    _pl_recovery_attempt=$((_pl_recovery_attempt + 1))
    echo "PRELOOP_STREAM_RECOVERY {agent_label} attempt=$_pl_recovery_attempt"
    sleep $(({backoff_seconds} * (1 << (_pl_recovery_attempt - 1))))
    echo '{encoded}' | base64 -d > {shlex.quote(prompt_path)}
    : > {shlex.quote(attempt_log_path)}
    PRELOOP_RECOVERY_TIMEOUT="timeout -k {kill_after_seconds} {timeout_seconds}"
    set +e
    {resume_command}
    set -e
    echo "PRELOOP_STREAM_RECOVERY_RESULT {agent_label} exit=${{{exit_code_var}}}"
done
"""
