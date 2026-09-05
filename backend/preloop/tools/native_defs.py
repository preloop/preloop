"""Catalogue of native agent tools shown on the Tools page.

Parameter names match the fields the permission hooks send through
``POST /api/v1/agents/permission-check`` and ``permission_prompt``
(``command``, ``file_path``, ``content``, ``url``, and so on). The list
endpoint copies ``parameters`` onto each row so the rule editor can
populate its field dropdown.
"""

from __future__ import annotations

from typing import Any, Dict, List

ADAPTER_CLAUDE_CODE = "Claude Code"
ADAPTER_CODEX_CLI = "Codex CLI"
ADAPTER_CURSOR = "Cursor"
ADAPTER_OPENCODE = "OpenCode"


def _string(description: str) -> Dict[str, str]:
    return {"type": "string", "description": description}


def _boolean(description: str) -> Dict[str, str]:
    return {"type": "boolean", "description": description}


def _integer(description: str) -> Dict[str, str]:
    return {"type": "integer", "description": description}


def _object(description: str) -> Dict[str, str]:
    return {"type": "object", "description": description}


NATIVE_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "Bash",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Run a shell command inside the Claude Code agent.",
        "parameters": {
            "command": _string("Shell command to execute"),
            "description": _string("Short explanation of the command"),
            "timeout": _integer("Optional timeout in milliseconds"),
        },
    },
    {
        "name": "Edit",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_CURSOR, ADAPTER_OPENCODE],
        "description": "Replace text in an existing file.",
        "parameters": {
            "file_path": _string("Path of the file to edit"),
            "old_string": _string("Text to find"),
            "new_string": _string("Replacement text"),
            "replace_all": _boolean("Replace every occurrence when true"),
        },
    },
    {
        "name": "Write",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_CURSOR, ADAPTER_OPENCODE],
        "description": "Create or overwrite a file.",
        "parameters": {
            "file_path": _string("Path of the file to write"),
            "content": _string("File contents"),
        },
    },
    {
        "name": "Read",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Read a file from the workspace.",
        "parameters": {
            "file_path": _string("Path of the file to read"),
        },
    },
    {
        "name": "Glob",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Find files by a glob pattern.",
        "parameters": {
            "pattern": _string("Glob pattern to match"),
            "path": _string("Directory to search"),
        },
    },
    {
        "name": "Grep",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Search file contents by pattern.",
        "parameters": {
            "pattern": _string("Search pattern"),
            "path": _string("Directory or file to search"),
        },
    },
    {
        "name": "WebFetch",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Fetch a URL from the agent process.",
        "parameters": {
            "url": _string("URL to fetch"),
        },
    },
    {
        "name": "NotebookEdit",
        "adapters": [ADAPTER_CLAUDE_CODE],
        "description": "Edit a cell in a Jupyter notebook.",
        "parameters": {
            "notebook_path": _string("Path of the notebook"),
            "cell_id": _string("Cell identifier"),
            "new_source": _string("Replacement cell source"),
            "cell_type": _string("Cell type (code or markdown)"),
            "edit_mode": _string("How to apply the edit"),
        },
    },
    {
        "name": "MultiEdit",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Apply several replacements to one file.",
        "parameters": {
            "file_path": _string("Path of the file to edit"),
            "edits": _object("List of old/new string replacements"),
        },
    },
    {
        "name": "Task",
        "adapters": [ADAPTER_CLAUDE_CODE, ADAPTER_OPENCODE],
        "description": "Launch a Claude Code sub-agent task.",
        "parameters": {
            "description": _string("Short task title"),
            "prompt": _string("Instructions for the sub-agent"),
            "subagent_type": _string("Kind of sub-agent to run"),
        },
    },
    {
        "name": "shell",
        "adapters": [ADAPTER_CODEX_CLI],
        "description": "Run a shell command inside Codex CLI.",
        "parameters": {
            "command": _string("Shell command to execute"),
        },
    },
    {
        "name": "apply_patch",
        "adapters": [ADAPTER_CODEX_CLI],
        "description": "Apply a patch through Codex CLI.",
        "parameters": {
            "input": _string("Patch text the hook sends"),
        },
    },
    {
        "name": "Shell",
        "adapters": [ADAPTER_CURSOR],
        "description": "Run a shell command inside Cursor.",
        "parameters": {
            "command": _string("Shell command to execute"),
        },
    },
]

NATIVE_TOOL_NAMES = {tool["name"] for tool in NATIVE_TOOLS}
