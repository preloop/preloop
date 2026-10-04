"""warehouse-sim: a synthetic MCP server for artifact tests.

Six tools. Content shapes are MCP ``ContentBlock`` values (spec 2026-07-28):
``TextContent``, ``EmbeddedResource`` (``TextResourceContents``) and
``AudioContent``. No side effects: ids are derived from the arguments.
"""

from __future__ import annotations

import json
from typing import List, Union

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import (
    AudioContent,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
)

from . import data

INSTRUCTIONS = (
    "Synthetic warehouse simulator with two sites (nord, sued) and three "
    "shifts (early, late, night). All people, emails and phone numbers are "
    "fictional. Tools never change anything; ids are deterministic."
)

TranscriptBlocks = List[Union[TextContent, EmbeddedResource]]


def _transcript_blocks(site: str, shift: str) -> TranscriptBlocks:
    lang, summary = data.SUMMARIES[(site, shift)]
    return [
        TextContent(
            type="text",
            text=f"[{site}/{shift}, {lang}] {summary}",
        ),
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=data.TRANSCRIPT_URI.format(site=site, shift=shift),
                mimeType=data.VTT_MIME,
                text=data.read_transcript(site, shift),
            ),
        ),
    ]


def _guard(fn, *args):
    try:
        return fn(*args)
    except data.FixtureError as exc:
        raise ToolError(str(exc)) from exc


def get_transcript(site: str, shift: str) -> TranscriptBlocks:
    """Return the shift transcript for a site as a summary line plus WebVTT."""
    _guard(data.transcript_path, site, shift)
    return _transcript_blocks(site, shift)


def list_workflows(site: str) -> List[TextContent]:
    """List the BPMN workflows of a site."""
    workflows = _guard(data.list_workflows, site)
    return [
        TextContent(type="text", text=json.dumps({"workflows": workflows}, indent=2))
    ]


def propose_workflow_change(
    site: str, workflow_id: str, bpmn_diff: str, justification: str = ""
) -> List[TextContent]:
    """Propose a change to a site workflow. Returns a change id; changes nothing."""
    # justification is optional on purpose: behind the Preloop firewall the
    # argument is consumed by Preloop and not forwarded upstream
    # (backend/preloop/services/dynamic_fastmcp.py, _call_tool), so the
    # server must accept its absence.
    _guard(data.check_site, site)
    if workflow_id not in data.WORKFLOWS:
        raise ToolError(
            f"unknown workflow_id {workflow_id!r}; expected one of {list(data.WORKFLOWS)}"
        )
    if not bpmn_diff.strip():
        raise ToolError("bpmn_diff is empty")
    change_id = data.stable_id(
        "chg", site, workflow_id, bpmn_diff, justification.strip()
    )
    body = {
        "change_id": change_id,
        "site": site,
        "workflow_id": workflow_id,
        "status": "proposed",
        "justification_received": bool(justification.strip()),
    }
    return [TextContent(type="text", text=json.dumps(body))]


def create_task(site: str, title: str, body: str) -> List[TextContent]:
    """Create a follow-up task for a site. Returns a task id; stores nothing."""
    _guard(data.check_site, site)
    if not title.strip():
        raise ToolError("title is empty")
    task_id = data.stable_id("task", site, title.strip(), body)
    out = {"task_id": task_id, "site": site, "title": title.strip()}
    return [TextContent(type="text", text=json.dumps(out))]


def get_audio(site: str, shift: str) -> List[AudioContent]:
    """Return a short synthetic WAV clip for the shift (a tone, not speech)."""
    _guard(data.transcript_path, site, shift)
    return [
        AudioContent(
            type="audio", data=data.audio_b64(site, shift), mimeType=data.WAV_MIME
        )
    ]


def transcribe_audio(audio_ref: str) -> TranscriptBlocks:
    """Deterministically "transcribe" a clip from get_audio.

    audio_ref may be the clip URI (warehouse-sim://audio/<site>/<shift>.wav),
    "<site>/<shift>", the base64 data of the AudioContent, or its sha256.
    """
    site, shift = _guard(data.resolve_audio_ref, audio_ref)
    return _transcript_blocks(site, shift)


TOOLS = (
    get_transcript,
    list_workflows,
    propose_workflow_change,
    create_task,
    get_audio,
    transcribe_audio,
)


def build_server(host: str = "127.0.0.1", port: int = 8765) -> FastMCP:
    server = FastMCP(
        "warehouse-sim",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
    )
    for fn in TOOLS:
        server.tool(structured_output=False)(fn)
    return server
