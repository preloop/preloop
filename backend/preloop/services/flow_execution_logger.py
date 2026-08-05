"""Service for logging flow execution details including MCP tool usage and agent actions."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from preloop.utils.redaction import redact_dict
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

MCP_TOOL_PATTERNS = [
    re.compile(
        r"(?:calling\s+(?:(?:mcp\s+)?tool:?\s+)?|tool\s+call:?\s+|mcp\s+tool:?\s+|called\s+tool:?\s+)"
        r"(?<!/)\b(?P<server>[A-Za-z0-9._-]+)/(?P<tool>[A-Za-z0-9._:-]+)\b(?!/)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:using|running)\s+(?:(?:mcp\s+)?tool\s+)"
        r"(?<!/)\b(?P<server>[A-Za-z0-9._-]+)/(?P<tool>[A-Za-z0-9._:-]+)\b(?!/)",
        re.IGNORECASE,
    ),
]


class FlowExecutionLogger:
    """
    Service for tracking and logging flow execution details.

    Maintains structured logs of:
    - MCP tool calls (server, tool, args, results)
    - Agent actions (files created, commands run, etc.)
    - Execution milestones and state changes
    """

    def __init__(self):
        """Initialize the execution logger."""
        self.mcp_usage_logs: List[Dict[str, Any]] = []
        self.actions_taken: List[Dict[str, Any]] = []
        self.milestones: List[Dict[str, Any]] = []
        self.agent_output_lines: List[str] = []  # Store all agent output for summary

    def log_mcp_tool_call(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        status: str = "pending",
        result_summary: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """
        Log an MCP tool call.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool called
            arguments: Arguments passed to the tool
            status: Status of the call (pending, success, failed)
            result_summary: Summary of the result
            error: Error message if failed
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": redact_dict(arguments),
            "status": status,
            "result_summary": result_summary,
            "error": error,
        }
        self.mcp_usage_logs.append(log_entry)
        logger.debug(f"MCP tool call logged: {server_name}/{tool_name} - {status}")

    def log_agent_action(
        self,
        action_type: str,
        description: str,
        details: Optional[Dict[str, Any]] = None,
        status: str = "completed",
    ):
        """
        Log an agent action.

        Args:
            action_type: Type of action (e.g., "file_created", "command_executed", "api_called")
            description: Human-readable description
            details: Additional structured details
            status: Status of the action
        """
        action_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": action_type,
            "description": description,
            "details": details or {},
            "status": status,
        }
        self.actions_taken.append(action_entry)
        # Log only the action type and status: the description is derived from
        # agent output and must not be duplicated into service logs
        # (py/clear-text-logging-sensitive-data, alert 181).
        logger.debug(f"Agent action logged: {action_type} (status={status})")

    def log_milestone(
        self, milestone_name: str, details: Optional[Dict[str, Any]] = None
    ):
        """
        Log an execution milestone.

        Args:
            milestone_name: Name of the milestone (e.g., "agent_started", "task_completed")
            details: Additional details about the milestone
        """
        milestone_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "milestone": milestone_name,
            "details": details or {},
        }
        self.milestones.append(milestone_entry)
        logger.info(f"Milestone reached: {milestone_name}")

    def get_mcp_usage_logs(self) -> List[Dict[str, Any]]:
        """
        Get all MCP usage logs.

        Returns:
            List of MCP tool call logs
        """
        return self.mcp_usage_logs

    def get_actions_taken(self) -> List[Dict[str, Any]]:
        """
        Get all agent actions.

        Returns:
            List of agent action logs
        """
        return self.actions_taken

    def get_milestones(self) -> List[Dict[str, Any]]:
        """
        Get all execution milestones.

        Returns:
            List of milestone logs
        """
        return self.milestones

    def log_agent_output(self, line: str):
        """
        Log a line of agent output.

        The line is scrubbed of known credential formats before it is stored,
        because these lines become ``model_output_summary``, which is served
        over the API and rendered in the console (issue #173).

        Args:
            line: A single line of agent stdout/stderr
        """
        self.agent_output_lines.append(scrub_secrets(line) or "")

    def get_agent_output_lines(self) -> List[str]:
        """
        Get all agent output lines.

        Returns:
            List of agent output lines
        """
        return self.agent_output_lines

    def get_agent_output_summary(self, tail_lines: int = 50) -> Optional[str]:
        """
        Get a summary of the agent output (last N lines).

        Args:
            tail_lines: Number of last lines to include

        Returns:
            Combined output string or None if no output
        """
        if not self.agent_output_lines:
            return None
        # Get the last N lines
        lines_to_include = self.agent_output_lines[-tail_lines:]
        return "\n".join(lines_to_include)

    def get_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the execution logs.

        Returns:
            Summary dict with counts and key events
        """
        return {
            "total_mcp_calls": len(self.mcp_usage_logs),
            "successful_mcp_calls": sum(
                1 for log in self.mcp_usage_logs if log["status"] == "success"
            ),
            "failed_mcp_calls": sum(
                1 for log in self.mcp_usage_logs if log["status"] == "failed"
            ),
            "total_actions": len(self.actions_taken),
            "milestones_reached": len(self.milestones),
            "last_milestone": self.milestones[-1] if self.milestones else None,
        }

    def parse_agent_logs(self, log_lines: List[str]):
        """
        Parse agent log lines to extract structured information.

        This is a heuristic parser that looks for common patterns in agent logs
        to extract actions and MCP calls.

        Args:
            log_lines: Raw log lines from the agent
        """
        for line in log_lines:
            # Look for MCP tool call patterns
            # Example: "Calling MCP tool: preloop-mcp/search_issues with args: {...}"
            line_lower = line.lower()
            if (
                "mcp" in line_lower
                or "tool call" in line_lower
                or "called tool" in line_lower
                or ("calling" in line_lower and "/" in line)
            ):
                self._try_extract_mcp_call(line)

            # Look for action patterns
            # Example: "Created file: /path/to/file.py"
            # Example: "Executed command: npm install"
            if "created file" in line.lower():
                self._try_extract_file_creation(line)
            elif "executed command" in line.lower() or "running:" in line.lower():
                self._try_extract_command_execution(line)

    def _try_extract_mcp_call(self, line: str):
        """Try to extract MCP call information from a log line."""
        try:
            normalized_line = line.strip()
            if not normalized_line:
                return

            for pattern in MCP_TOOL_PATTERNS:
                match = pattern.search(normalized_line)
                if not match:
                    continue

                server_name = match.group("server").strip()
                tool_name = match.group("tool").strip()
                if not self._looks_like_mcp_identifier(server_name, tool_name):
                    continue

                self.log_mcp_tool_call(
                    server_name=server_name,
                    tool_name=tool_name,
                    arguments={},
                    status="detected",
                )
                return
        except Exception as e:
            # Do not echo the line itself: it is agent output and belongs only
            # in the scrubbed execution log, not in service logs (alert 182).
            logger.debug(f"Could not parse MCP call from line (len={len(line)}): {e}")

    def _looks_like_mcp_identifier(self, server_name: str, tool_name: str) -> bool:
        """Return True when the parsed server/tool pair looks like a real MCP call."""
        if not server_name or not tool_name:
            return False
        if server_name.lower() in {"http", "https"}:
            return False
        if re.fullmatch(r"v\d+", tool_name, re.IGNORECASE):
            return False
        if "://" in server_name or "://" in tool_name:
            return False
        return True

    def _try_extract_file_creation(self, line: str):
        """Try to extract file creation information from a log line."""
        try:
            # Basic pattern: "Created file: /path/to/file.py"
            if ":" in line:
                file_path = line.split(":", 1)[-1].strip()
                self.log_agent_action(
                    action_type="file_created",
                    description=f"Created file: {file_path}",
                    details={"file_path": file_path},
                )
        except Exception as e:
            # Do not echo the line itself (alert 183), see _try_extract_mcp_call.
            logger.debug(
                f"Could not parse file creation from line (len={len(line)}): {e}"
            )

    def _try_extract_command_execution(self, line: str):
        """Try to extract command execution information from a log line."""
        try:
            # Basic pattern: "Executed command: npm install"
            if ":" in line:
                command = line.split(":", 1)[-1].strip()
                self.log_agent_action(
                    action_type="command_executed",
                    description=f"Executed: {command}",
                    details={"command": command},
                )
        except Exception as e:
            logger.debug(
                f"Could not parse command execution from line (len={len(line)}): {e}"
            )
