"""Audio transcription agent preset (020, #1103).

Two parts:

* The shipped YAML: identity, on-demand trigger, allowlists, and the prompt
  rules the issue requires (labels, VTT, lineage, consent, audio refusal).
* The contract end to end: a scripted agent performs the prompt's steps
  against the in-process Preloop MCP endpoint (deposit_artifact, ask_user)
  and the warehouse-sim fixture's get_audio / transcribe_audio (#1091).
  No model is involved; the test proves the platform side of every step
  the prompt asks for.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from preloop.flow_presets import load_flow_presets
from preloop.models import models
from preloop.models.schemas.flow import FlowCreate
from preloop.services import artifact_deposit
from tests.api.test_artifact_deposit import _session, _token
from tests.api.test_deposit_artifact_mcp import _drop_async_pool, _enable, _mcp

REPO = Path(__file__).resolve().parents[2]
PRESET_PATH = REPO / "backend" / "presets" / "020-audio-transcription-agent.yaml"
PICKER_TEST = (
    REPO / "frontend" / "src" / "components" / "preloop-flow-preset-picker.test.ts"
)
SLUG = "audio-transcription-agent"
AUDIO_SERVER = "audio-mcp"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.fixtures.warehouse_sim import server as sim  # noqa: E402


@pytest.fixture
def shared_db(db_session, monkeypatch):
    """Every get_db the MCP stack opens joins the test transaction."""
    from sqlalchemy.orm import Session

    def _factory():
        session = Session(bind=db_session.connection())
        try:
            yield session
        finally:
            session.close()

    for target in (
        "preloop.models.db.session.get_db_session",
        "preloop.services.mcp_http.get_db",
        "preloop.services.dynamic_fastmcp.get_db",
    ):
        monkeypatch.setattr(target, _factory)
    _drop_async_pool()
    yield db_session
    _drop_async_pool()


@pytest.fixture(scope="module")
def preset() -> dict[str, Any]:
    return yaml.safe_load(PRESET_PATH.read_text())


@pytest.fixture(scope="module")
def raw() -> str:
    return PRESET_PATH.read_text()


# --- the YAML ---------------------------------------------------------------


def test_identity_and_on_demand_trigger(preset):
    assert preset["slug"] == SLUG
    assert preset["is_enabled"] is False
    assert preset["trigger_event_source"] is None
    assert preset["trigger_event_types"] is None
    assert preset["is_preset"] is True


def test_schedule_variant_and_run_flow_example_are_commented(raw):
    assert "# trigger_event_source: schedule" in raw
    assert "#   expr:" in raw
    assert 'run_flow(flow="audio-transcription-agent"' in raw
    for field in ("site", "shift", "audio_ref", "consent_basis"):
        assert f'"{field}"' in raw


def test_allowlists_name_preloop_and_the_audio_server(preset, raw):
    assert preset["allowed_mcp_servers"] == ["preloop-mcp", AUDIO_SERVER]
    builtins = {t["name"] for t in preset["allowed_mcp_tools"] if "name" in t}
    external = {
        (t["server_name"], t["tool_name"])
        for t in preset["allowed_mcp_tools"]
        if "server_name" in t
    }
    assert builtins == {"deposit_artifact", "ask_user"}
    assert external == {(AUDIO_SERVER, "get_audio"), (AUDIO_SERVER, "transcribe_audio")}
    assert "Template variable audio_mcp_server (default: audio-mcp)" in raw
    assert "AudioContent" in raw


def test_prompt_states_every_rule_the_issue_requires(preset):
    prompt = preset["prompt_template"]
    assert "{{trigger_event.payload}}" in prompt
    assert 'kind: "transcript"' in prompt
    assert "WebVTT preferred" in prompt
    assert '"site": <site>, "shift": <shift>' in prompt
    assert '"consent_basis": <consent_basis>' in prompt
    assert 'kind: "document"' in prompt
    assert "parent_artifact_id: the transcript id" in prompt
    assert artifact_deposit.ERROR_AUDIO_DISABLED in prompt
    assert "do not treat it as a failure" in " ".join(prompt.split())
    consent = prompt.index("STEP 0. CONSENT FIRST")
    assert consent < prompt.index("STEP 1. FETCH")
    assert "Call ask_user" in prompt[consent : prompt.index("STEP 1. FETCH")]
    assert "does not verify" in prompt


def test_card_copy_says_preloop_does_not_verify_consent(preset):
    first = re.split(r"(?<=[.!?])\s", " ".join(preset["description"].split()))[0]
    assert "does not verify the consent basis" in first
    assert len(first) <= 120, "keep the caveat visible in the gallery row"
    # The gallery browser test pins the same first sentence.
    joined = re.sub(r"['\"]\s*\+\s*['\"]", "", PICKER_TEST.read_text())
    assert first in " ".join(joined.split())


def test_loader_ships_it_and_the_flow_schema_accepts_it(preset):
    load_flow_presets.cache_clear()
    loaded = {p["name"]: p for p in load_flow_presets()}
    assert preset["name"] in loaded
    body = {
        k: v
        for k, v in preset.items()
        if k not in {"slug", "is_preset", "trigger_config"}
    }
    flow = FlowCreate(**body)
    assert flow.allowed_mcp_servers == ["preloop-mcp", AUDIO_SERVER]


# --- the contract, end to end -----------------------------------------------


def _sim_blocks(blocks) -> list[dict[str, Any]]:
    return [b.model_dump(mode="json", exclude_none=True) for b in blocks]


async def _run_contract(mcp, payload: dict[str, Any]) -> dict[str, Any]:
    """The prompt's steps, scripted. Returns what the agent would report."""
    for field in ("site", "shift", "audio_ref", "consent_basis"):
        if not str(payload.get(field) or "").strip():
            question = (
                f"No {field} was given for site {payload.get('site')}, "
                f"shift {payload.get('shift')}. What is the {field} for this "
                "recording?"
            )
            asked = asyncio.create_task(
                mcp.call_tool(
                    "ask_user",
                    {"question": question, "context": str(payload)},
                )
            )
            return {"asked": question, "task": asked}
    labels = {
        "site": payload["site"],
        "shift": payload["shift"],
        "consent_basis": payload["consent_basis"],
    }
    audio = _sim_blocks(sim.get_audio(payload["site"], payload["shift"]))[0]
    blocks = _sim_blocks(sim.transcribe_audio(payload["audio_ref"]))
    vtt = next(b for b in blocks if b["type"] == "resource")
    assert vtt["resource"]["mimeType"] == "text/vtt"
    tag = f"{payload['site']}-{payload['shift']}"
    transcript = await mcp.call_tool(
        "deposit_artifact",
        {"name": f"{tag}.vtt", "kind": "transcript", "content": vtt, "labels": labels},
    )
    assert not transcript.isError, transcript.content
    transcript_id = transcript.structuredContent["id"]
    summary = await mcp.call_tool(
        "deposit_artifact",
        {
            "name": f"{tag}-summary.md",
            "kind": "document",
            "content": {"type": "text", "text": "# Summary\n\n- Damaged pallet.\n"},
            "labels": labels,
            "parent_artifact_id": transcript_id,
        },
    )
    assert not summary.isError, summary.content
    raw_audio = await mcp.call_tool(
        "deposit_artifact",
        {
            "name": f"{tag}.wav",
            "content": audio,
            "labels": labels,
            "parent_artifact_id": transcript_id,
        },
    )
    audio_text = " ".join(getattr(c, "text", "") for c in raw_audio.content)
    if raw_audio.isError:
        # Expected when the account has not opted in; never a failure.
        assert artifact_deposit.ERROR_AUDIO_DISABLED in audio_text
    return {
        "transcript_id": transcript_id,
        "summary_id": summary.structuredContent["id"],
        "audio_stored": not raw_audio.isError,
        "succeeded": True,
    }


def _artifacts(db, session_id) -> dict[str, models.RuntimeSessionArtifact]:
    db.expire_all()
    rows = db.scalars(
        select(models.RuntimeSessionArtifact).where(
            models.RuntimeSessionArtifact.runtime_session_id == session_id
        )
    ).all()
    return {row.kind: row for row in rows}


PAYLOAD = {
    "site": "nord",
    "shift": "late",
    "audio_ref": "nord/late",
    "consent_basis": "works-agreement-2026-03",
}
LABELS = {k: PAYLOAD[k] for k in ("site", "shift", "consent_basis")}


def _default_question_workflow(db, user) -> None:
    """What account setup creates for every real account (OSS default)."""
    from preloop.models.crud import crud_approval_workflow
    from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE

    crud_approval_workflow.create(
        db,
        obj_in={
            "name": "Default Approval Workflow",
            "approval_type": DEFAULT_APPROVAL_TYPE,
            "approval_mode": "standard",
            "is_default": True,
            "approvals_required": 1,
            "approver_user_ids": [str(user.id)],
        },
        account_id=str(user.account_id),
    )
    db.flush()


def _set_audio(db, account_id, enabled: bool) -> None:
    account = db.get(models.Account, account_id)
    account.meta_data = {
        **(account.meta_data or {}),
        "artifacts": {"audio_storage_enabled": enabled},
    }
    db.flush()


@pytest.mark.asyncio
async def test_audio_off_transcript_and_summary_with_labels_and_lineage(
    app, shared_db, test_user
):
    db = shared_db
    session = _session(db, test_user.account_id, "audio-preset-off")
    token = _token(db, test_user, runtime_session_id=session.id)
    _enable(db, test_user.account_id)

    async with _mcp(app, token) as mcp:
        report = await _run_contract(mcp, PAYLOAD)

    assert report["succeeded"] is True
    assert report["audio_stored"] is False
    rows = _artifacts(db, session.id)
    assert set(rows) == {"transcript", "document"}
    assert rows["transcript"].content_type == "text/vtt"
    assert rows["transcript"].labels == LABELS
    assert rows["document"].labels == LABELS
    assert rows["document"].parent_artifact_id == rows["transcript"].id


@pytest.mark.asyncio
async def test_audio_on_stores_the_audio_with_lineage(app, shared_db, test_user):
    db = shared_db
    _set_audio(db, test_user.account_id, True)
    session = _session(db, test_user.account_id, "audio-preset-on")
    token = _token(db, test_user, runtime_session_id=session.id)
    _enable(db, test_user.account_id)

    async with _mcp(app, token) as mcp:
        report = await _run_contract(mcp, PAYLOAD)

    assert report["audio_stored"] is True
    rows = _artifacts(db, session.id)
    assert set(rows) == {"transcript", "document", "audio"}
    assert rows["audio"].content_type == "audio/wav"
    assert rows["audio"].labels == LABELS
    assert rows["audio"].parent_artifact_id == rows["transcript"].id


@pytest.mark.asyncio
async def test_missing_consent_asks_the_user_and_deposits_nothing(
    app, shared_db, test_user, monkeypatch
):
    """ask_user reaches the approval path as a question; nothing is stored.

    ``require_approval`` is stubbed: it runs on the async engine, which cannot
    see this test's open transaction. That the resulting pending question is
    listed in Attention is covered by the ask_user and attention suites; the
    local-stack run in the PR shows it for this preset.
    """
    db = shared_db
    session = _session(db, test_user.account_id, "audio-preset-no-consent")
    token = _token(db, test_user, runtime_session_id=session.id)
    _enable(db, test_user.account_id)
    _default_question_workflow(db, test_user)
    asked: list[dict[str, Any]] = []

    async def _pending(**kwargs):
        asked.append(kwargs)
        return False, '{"status": "pending_approval", "request_id": "q-1"}'

    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", _pending)

    async with _mcp(app, token) as mcp:
        report = await _run_contract(mcp, {**PAYLOAD, "consent_basis": ""})
        result = await asyncio.wait_for(report["task"], timeout=30)

    assert not result.isError, result.content
    assert "pending_approval" in result.content[0].text
    assert len(asked) == 1
    assert asked[0]["tool_name"] == "ask_user"
    assert asked[0]["arguments"]["is_question"] is True
    assert "No consent_basis was given" in asked[0]["arguments"]["question"]
    assert _artifacts(db, session.id) == {}
