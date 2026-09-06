"""Bounded PR metadata and publisher-owned execution provenance.

This module uses only the standard library so the legacy container wrapper can
carry the same validation implementation without installing the server package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID

MAX_TITLE_BYTES = 256
MAX_BODY_BYTES = 60000
MAX_ARTIFACT_BYTES = 256 * 1024
PROVENANCE_START = "<!-- preloop:executions:start -->"
PROVENANCE_END = "<!-- preloop:executions:end -->"
_PROVENANCE = re.compile(
    re.escape(PROVENANCE_START) + r".*?" + re.escape(PROVENANCE_END), re.DOTALL
)


@dataclass(frozen=True)
class PublicationRecord:
    """An execution and exact published head from trusted persistence."""

    execution_id: str
    head_sha: str

    def __post_init__(self) -> None:
        UUID(self.execution_id)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.head_sha):
            raise ValueError("Publication record requires a full commit SHA")


def bounded_text(value: Any, *, title: bool = False) -> str:
    """Return valid metadata, or empty text so the next source can be tried."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    limit = MAX_TITLE_BYTES if title else MAX_BODY_BYTES
    if len(value.encode("utf-8")) > limit:
        return ""
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        return ""
    if title and ("\n" in value or "\t" in value):
        return ""
    # An agent must not impersonate the publisher-owned region.
    if PROVENANCE_START in value or PROVENANCE_END in value:
        return ""
    return value


def read_result_metadata(raw: bytes | None) -> tuple[str, str, list[str]]:
    """Read #420 aliases without coercion or unbounded JSON parsing."""
    if raw is None:
        return (
            "",
            "",
            ["PR metadata artifact missing; using configured/commit fallback"],
        )
    try:
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("oversized")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeDecodeError):
        return (
            "",
            "",
            ["PR metadata artifact invalid; using configured/commit fallback"],
        )
    nested = data.get("pull_request")
    nested = nested if isinstance(nested, dict) else {}
    title = next(
        (
            text
            for value in (
                data.get("pr_title"),
                data.get("pull_request_title"),
                nested.get("title"),
            )
            if (text := bounded_text(value, title=True))
        ),
        "",
    )
    body = next(
        (
            text
            for value in (
                data.get("pr_body"),
                data.get("pr_description"),
                data.get("pull_request_description"),
                nested.get("body"),
                nested.get("description"),
            )
            if (text := bounded_text(value))
        ),
        "",
    )
    warnings = []
    if not title or not body:
        warnings.append("PR metadata incomplete or invalid; using per-field fallback")
    return title, body, warnings


def select_metadata(
    raw: bytes | None,
    *,
    configured_title: str = "",
    configured_body: str = "",
    commit_title: str = "",
    commit_body: str = "",
    issue_number: str = "",
) -> tuple[str, str, list[str]]:
    """Apply agent, configuration, commit precedence independently per field."""
    title, body, warnings = read_result_metadata(raw)
    title = (
        title
        or bounded_text(configured_title, title=True)
        or bounded_text(commit_title, title=True)
        or "Automated changes"
    )
    body = (
        body
        or bounded_text(configured_body)
        or bounded_text(commit_body)
        or "## Summary\n\nAutomated repository changes.\n\n## Testing\n\nNo test evidence supplied."
    )
    if issue_number and re.fullmatch(r"[1-9][0-9]*", issue_number):
        if not re.search(rf"(?<![0-9])#{issue_number}(?![0-9])", body):
            body += f"\n\nCloses #{issue_number}"
    return title, body, warnings


def provenance_block(records: Sequence[PublicationRecord], public_url: str) -> str:
    """Build links only from the public application URL and trusted UUIDs."""
    parsed = urlsplit(public_url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Public application URL must not contain credentials or query data"
        )
    if any(char in public_url for char in "\n\r<>()[] "):
        raise ValueError("Invalid public application URL")
    unique = list(dict.fromkeys(records))
    if not unique or len(unique) > 200:
        raise ValueError("Publication requires between 1 and 200 execution records")
    lines = [PROVENANCE_START, "### Preloop executions", ""]
    for index, record in enumerate(unique):
        label = "Initial execution" if index == 0 else "Repair execution"
        url = f"{public_url.rstrip('/')}/console/flows/executions/{record.execution_id}"
        lines.append(f"- [{label}]({url}) — published `{record.head_sha}`")
    return "\n".join([*lines, PROVENANCE_END])


def upsert_provenance(
    body: str, records: Sequence[PublicationRecord], public_url: str
) -> str:
    """Replace only the owned region, preserving human edits byte for byte."""
    block = provenance_block(records, public_url)
    # Malformed delimiters are ambiguous ownership: fail without erasing text.
    matches = list(_PROVENANCE.finditer(body))
    if body.count(PROVENANCE_START) != len(matches) or body.count(
        PROVENANCE_END
    ) != len(matches):
        raise ValueError(
            "Malformed publisher provenance region; repair delimiters first"
        )
    if matches:
        first = matches[0]
        result = body[: first.start()] + block + body[first.end() :]
        # Remove duplicate *owned* blocks only; all outside text stays intact.
        offset = first.start() + len(block)
        result = result[:offset] + _PROVENANCE.sub("", result[offset:])
    else:
        result = body + ("\n\n" if body else "") + block
    if len(result.encode("utf-8")) > 65536:
        raise ValueError("PR body plus provenance exceeds provider limit")
    return result


def discover_template(
    files: Mapping[str, str], *, provider: str, configured: str | None = None
) -> tuple[str | None, str]:
    """Select configured template, conventional default, then lexical first.

    ``files`` must come from repository text blobs, never executable metadata.
    The fallback for no template intentionally leaves all checkboxes unchecked.
    """
    if configured:
        path = Path(configured)
        if path.is_absolute() or ".." in path.parts or configured not in files:
            raise ValueError("Configured PR template is missing or outside repository")
        selected = configured
    else:
        defaults = (
            [
                ".gitlab/merge_request_templates/Default.md",
                ".gitlab/merge_request_templates/default.md",
            ]
            if provider == "gitlab"
            else [
                ".github/pull_request_template.md",
                ".github/PULL_REQUEST_TEMPLATE.md",
                "pull_request_template.md",
                "PULL_REQUEST_TEMPLATE.md",
                "docs/pull_request_template.md",
                "docs/PULL_REQUEST_TEMPLATE.md",
            ]
        )
        selected = next((path for path in defaults if path in files), None)
        if selected is None:
            prefix = (
                ".gitlab/merge_request_templates/"
                if provider == "gitlab"
                else ".github/PULL_REQUEST_TEMPLATE/"
            )
            selected = next(
                iter(
                    sorted(
                        path
                        for path in files
                        if path.startswith(prefix) and path.lower().endswith(".md")
                    )
                ),
                None,
            )
    if selected is None:
        return None, "## Summary\n\n## Testing\n"
    text = bounded_text(files[selected])
    if not text:
        raise ValueError("PR template is empty, invalid, or exceeds metadata limit")
    return selected, files[selected]


def repository_template(
    root: Path, *, provider: str, configured: str | None = None
) -> tuple[str | None, str]:
    """Read bounded, in-repository template files without following escapes."""
    root = root.resolve()
    candidates = (
        [configured]
        if configured
        else [
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            "pull_request_template.md",
            "PULL_REQUEST_TEMPLATE.md",
            "docs/pull_request_template.md",
            "docs/PULL_REQUEST_TEMPLATE.md",
            ".gitlab/merge_request_templates/Default.md",
            ".gitlab/merge_request_templates/default.md",
        ]
    )
    if not configured:
        directory = root / (
            ".gitlab/merge_request_templates"
            if provider == "gitlab"
            else ".github/PULL_REQUEST_TEMPLATE"
        )
        if directory.is_dir() and directory.resolve().is_relative_to(root):
            candidates += [
                str(path.relative_to(root)) for path in directory.glob("*.md")
            ][:256]
    files: dict[str, str] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        path = root / candidate
        if path.is_file() and path.resolve().is_relative_to(root):
            with path.open("rb") as stream:
                raw = stream.read(MAX_BODY_BYTES + 1)
            if len(raw) <= MAX_BODY_BYTES:
                try:
                    files[candidate] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
    return discover_template(files, provider=provider, configured=configured)
