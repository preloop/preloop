#!/usr/bin/env python3
"""Validate server.json for the official MCP registry.

Checks the repo-root ``server.json`` against the vendored registry schema
(scripts/schemas/mcp-server.schema.<date>.json, downloaded from
static.modelcontextprotocol.io), and that its version matches VERSION. The
registry rejects a re-publish of an existing version, so the two must move
together (scripts/release.py bumps both).

With ``--tag vX.Y.Z`` it also checks the release tag (the release workflow's
MCP registry publish job runs it). A stable tag must equal the server.json
version or the check fails. A pre-release tag (one with a ``-`` suffix) whose
version server.json does not carry is skipped, not failed: pre-releases are
not published to the registry unless server.json was bumped to them.

Usage:
    python scripts/validate_server_json.py [--tag vX.Y.Z] [path/to/server.json]
    python scripts/validate_server_json.py --is-duplicate-publish <output-file>
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft7Validator, FormatChecker

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT_DIR / "scripts" / "schemas"
#: jsonschema skips ``format: uri`` unless an optional package is installed,
#: so the check is explicit here: an absolute URI with a scheme.
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("uri")
def _is_uri(value: object) -> bool:
    """Return whether a string is an absolute URI.

    Args:
        value: The instance being validated.

    Returns:
        True for non-strings (type checks handle them) and absolute URIs.
    """
    if not isinstance(value, str):
        return True
    parsed = urlparse(value)
    return (
        bool(parsed.scheme) and bool(parsed.netloc or parsed.path) and " " not in value
    )


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
        for err in Draft7Validator(schema, format_checker=FORMAT_CHECKER).iter_errors(
            server
        )
    ]
    if server.get("version") != version:
        problems.append(
            f"version {server.get('version')!r} does not match VERSION {version!r}"
        )
    return problems


#: How the registry reports a re-publish of an existing version
#: (``<name>@<version> already exists``, a ``duplicate version`` error, or an
#: HTTP 409). A bare ``409`` is not enough: it could be part of a version.
DUPLICATE_PUBLISH = re.compile(
    r"already exists|duplicate version|(?:status|HTTP)[^0-9\n]{0,10}409\b",
    re.IGNORECASE,
)


def is_duplicate_publish(output: str) -> bool:
    """Return whether ``mcp-publisher publish`` failed only on a duplicate.

    Args:
        output: Combined stdout and stderr of the failed publish.

    Returns:
        True when the registry refused because the version already exists.
    """
    return DUPLICATE_PUBLISH.search(output) is not None


def check_tag(server_version: object, tag: str) -> tuple[bool, str | None]:
    """Decide whether a release tag should publish this server.json.

    Args:
        server_version: The ``version`` field of server.json.
        tag: The git tag, e.g. ``v0.17.1`` or ``v0.18.0-rc.1``.

    Returns:
        ``(publish, problem)``. ``problem`` is set when the tag and the
        version disagree on a stable tag, which must fail the job.
        ``publish`` is False for a mismatch, including the pre-release case
        where the mismatch is expected and is not a problem.
    """
    tag_version = tag[1:] if tag.startswith("v") else tag
    if server_version == tag_version:
        return True, None
    if "-" in tag_version:
        return False, None
    return False, (
        f"server.json version {server_version!r} does not match tag {tag!r} "
        f"(expected {tag_version!r}); run scripts/release.sh {tag_version}"
    )


def main(argv: list[str]) -> int:
    """Validate the given (or repo-root) server.json.

    Args:
        argv: Command-line arguments without the program name: an optional
            ``--tag <tag>`` pair and an optional server.json path, or
            ``--is-duplicate-publish <file>`` to classify publish output.

    Returns:
        Process exit code: 0 when valid (or a duplicate publish), 1 otherwise,
        2 for a malformed ``--is-duplicate-publish`` call.
    """
    args = list(argv)
    if args[:1] == ["--is-duplicate-publish"]:
        if len(args) != 2:
            print("--is-duplicate-publish needs a file", file=sys.stderr)
            return 2
        text = Path(args[1]).read_text(encoding="utf-8", errors="replace")
        return 0 if is_duplicate_publish(text) else 1
    tag: str | None = None
    if "--tag" in args:
        index = args.index("--tag")
        if index + 1 >= len(args):
            print("--tag needs a value", file=sys.stderr)
            return 1
        tag = args[index + 1]
        del args[index : index + 2]
    path = Path(args[0]) if args else ROOT_DIR / "server.json"
    version = (ROOT_DIR / "VERSION").read_text(encoding="utf-8").strip()
    server = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(server, version)
    publish = False
    if tag is not None:
        publish, tag_problem = check_tag(server.get("version"), tag)
        if tag_problem:
            problems.append(tag_problem)
        elif not publish:
            print(
                f"{path}: pre-release tag {tag} is not server.json version "
                f"{server.get('version')!r}; not publishing"
            )
        output = os.environ.get("GITHUB_OUTPUT")
        if output and not problems:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write(f"publish={'true' if publish else 'false'}\n")
    for problem in problems:
        print(f"{path}: {problem}", file=sys.stderr)
    if not problems:
        print(f"{path}: valid")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
