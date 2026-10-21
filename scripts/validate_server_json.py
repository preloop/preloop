#!/usr/bin/env python3
"""Validate server.json for the official MCP registry.

Checks the repo-root ``server.json`` against the vendored registry schema
(scripts/schemas/mcp-server.schema.<date>.json, downloaded from
static.modelcontextprotocol.io), and that its version matches VERSION. The
registry rejects a re-publish of an existing version, so the two must move
together (scripts/release.py bumps both).

Usage:
    python scripts/validate_server_json.py [path/to/server.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT_DIR / "scripts" / "schemas"
SCHEMA_URL = re.compile(
    r"^https://static\.modelcontextprotocol\.io/schemas/"
    r"(?P<date>\d{4}-\d{2}-\d{2})/server\.schema\.json$"
)


def validate(server: dict[str, Any], version: str) -> list[str]:
    """Return every problem with a server.json document.

    Args:
        server: The parsed server.json.
        version: The release version from VERSION.

    Returns:
        Human-readable problems; empty when the document is valid.
    """
    match = SCHEMA_URL.match(str(server.get("$schema", "")))
    if match is None:
        return ["$schema must be a static.modelcontextprotocol.io server schema URL"]
    schema_path = SCHEMA_DIR / f"mcp-server.schema.{match['date']}.json"
    if not schema_path.is_file():
        return [f"no vendored schema for {match['date']} at {schema_path}"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    problems = [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in Draft7Validator(schema).iter_errors(server)
    ]
    if server.get("version") != version:
        problems.append(
            f"version {server.get('version')!r} does not match VERSION {version!r}"
        )
    return problems


def main(argv: list[str]) -> int:
    """Validate the given (or repo-root) server.json.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        Process exit code: 0 when valid, 1 otherwise.
    """
    path = Path(argv[0]) if argv else ROOT_DIR / "server.json"
    version = (ROOT_DIR / "VERSION").read_text(encoding="utf-8").strip()
    problems = validate(json.loads(path.read_text(encoding="utf-8")), version)
    for problem in problems:
        print(f"{path}: {problem}", file=sys.stderr)
    if not problems:
        print(f"{path}: valid")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
