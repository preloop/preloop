"""CodeQL py/clear-text-logging-sensitive-data: agent output stays out of logs.

stream_logs yields container output that CodeQL taints as secret (env
credentials). Application logs may record counts and lengths, not the line.
"""

from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _BACKEND / "preloop" / "services" / "flow_orchestrator.py"
_BINDING = _BACKEND / "preloop" / "services" / "flow_pr_binding.py"


def test_orchestrator_does_not_log_agent_line_bodies() -> None:
    source = _ORCHESTRATOR.read_text(encoding="utf-8")
    assert "Streamed log line #%s (%s chars)" in source
    assert "{log_line" not in source
    assert "Previous line: {previous_line" not in source
    assert "Previous line: %s" not in source
    assert "for the completion contract: %s" not in source
    assert "Agent reported a CLI session" in source
    assert "Publication gate evidence captured" in source
    assert "Publication gate evidence captured:" not in source


def test_binding_does_not_log_cli_session_fields() -> None:
    source = _BINDING.read_text(encoding="utf-8")
    assert 'logger.info("Recorded CLI session on execution %s", execution_id)' in source
