"""Opt-in local stack: real OpenCode, PostgreSQL, Git and encrypted checkpoints.

Run with OPENCODE_CONVERSATION_SMOKE=1 and OpenCode 1.18.29 on PATH.
The model endpoint is synthetic and runtime API handlers run in-process.
Fresh OpenCode servers and session homes represent container lifetimes.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents import opencode_conversation as driver
from preloop.agents.checkpoint_client import capture, restore
from preloop.agents.opencode import OpenCodeAgent
from preloop.agents.session_manifest import (
    pack_session,
    restore_session,
    select_session_files,
    unpack_session,
)
from preloop.api.endpoints import flow_conversations as api
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud import flow_conversation as crud
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.flow import FlowUpdate
from preloop.schemas.flow_conversation import RuntimeTurnUpdate
from preloop.services import flow_artifacts, flow_conversations as service
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from tests.agents.test_conversation_workspace_branch import context, git, run_shell
from tests.agents.test_conversation_workspace_branch import repository as repository
from tests.services.test_flow_conversations import thread as thread


@pytest.mark.skipif(
    os.environ.get("OPENCODE_CONVERSATION_SMOKE") != "1",
    reason="opt-in local OpenCode 1.18.29 conversation integration",
)
@pytest.mark.parametrize("with_repository", [True, False])
def test_idle_stop_and_two_resumes_produce_answers(
    db_session: Session,
    thread: models.FlowConversation,  # noqa: F811
    repository: tuple[Path, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_repository: bool,
) -> None:
    """Three runtime lifetimes preserve native history, checkout and turn count."""
    binary = shutil.which("opencode")
    assert binary, "Install OpenCode 1.18.29 for this integration test"
    assert (
        subprocess.check_output([binary, "--version"], text=True).strip() == "1.18.29"
    )
    for key in list(os.environ):
        if key.startswith(("OPENCODE_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key)
    for key in ("MODELS_FETCH", "AUTOUPDATE", "DEFAULT_PLUGINS", "CLAUDE_CODE"):
        monkeypatch.setenv("OPENCODE_DISABLE_" + key, "true")
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    primary_requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """Unbilled OpenAI-compatible upstream that reports received history."""

        def log_message(self, format: str, *args: Any) -> None:
            """Keep the fixture quiet."""

        def do_POST(self) -> None:  # noqa: N802
            """Answer from the native history sent by the actual OpenCode CLI."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("tools"):
                primary_requests.append(body)
            turns = sum(item["role"] == "user" for item in body["messages"])
            text = f"amber; user turns={turns}"
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [
                    ({"role": "assistant", "content": text}, None),
                    ({}, "stop"),
                ]:
                    chunk = {
                        "id": "chatcmpl_fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": finish}
                        ],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(0.1)
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "chatcmpl_fixture",
                            "object": "chat.completion",
                            "created": 1,
                            "model": "fixture",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ).encode()
                )
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    agent = OpenCodeAgent({})
    config = agent._build_opencode_config(
        "fixture",
        "custom",
        {
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model_api_protocol": "chat_completions",
        },
        60000,
    )
    config["mcp"] = {}
    config["provider"]["preloop"]["options"]["apiKey"] = "local-fixture"
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(config))
    upstream, source = repository
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_marker = tmp_path / "session-id"
    monkeypatch.setattr(
        driver,
        "Path",
        lambda path: session_marker
        if str(path) == "/tmp/preloop-cli-session-id"
        else Path(path),
    )
    crud.save(db_session, thread, idle_timeout_seconds=1)

    started = time.monotonic()

    def control(body: dict[str, Any] | None = None) -> Any:
        """Use real runtime endpoints and their database lifecycle transitions."""
        assert time.monotonic() - started < 60, (
            thread.state,
            thread.idle_timeout_seconds,
            thread.idle_deadline,
            datetime.now(UTC),
        )
        if body is None:
            return api.runtime_poll(row=thread, db=db_session)
        return api.runtime_update(RuntimeTurnUpdate(**body), row=thread, db=db_session)

    monkeypatch.setattr(driver, "control", control)
    native_archive = workspace_archive = None
    native_id = ""
    references = {}
    branch = head = None
    try:
        for turn in (1, 2, 3):
            prior_id = thread.execution_id
            if turn > 1:
                assert service.admission_notice(thread) is None
                service.resume_checkpoint(db_session, thread)
                crud.admit(
                    db_session,
                    thread,
                    text=f"Continue turn {turn}; what word?",
                    request_id=uuid4(),
                    starting=True,
                )
                execution = crud_flow_execution.create(
                    db_session,
                    FlowExecutionCreate(flow_id=thread.flow_id, status="RUNNING"),
                )
                crud.bind_execution(db_session, thread, execution.id)
            execution = crud_flow_execution.get(db_session, id=thread.execution_id)
            ctx = context(upstream, str(execution.id))
            ctx.update(account_id=str(thread.account_id), flow_id=str(thread.flow_id))
            ctx["trigger_event_data"]["_conversation_id"] = str(thread.id)
            if not with_repository:
                ctx["git_clone_config"] = {}
            session_home = tmp_path / f"runtime-{turn}"
            for name in ("CONFIG", "DATA", "CACHE", "STATE"):
                monkeypatch.setenv(f"XDG_{name}_HOME", str(session_home / name.lower()))
            if turn > 1:
                ctx["trigger_event_data"]["_resume"] = {"execution_id": str(prior_id)}
                native = service.bound_resume(db_session, ctx)
                assert native["session_id"] == native_id
                ctx["checkpoint_env"] = {"PRELOOP_CHECKPOINT_GET_TOKEN": "fixture"}
                assert workspace_archive is not None and native_archive is not None
                restored_archives = {
                    kind: flow_artifacts.get_artifact(
                        db_session,
                        account_id=thread.account_id,
                        flow_id=thread.flow_id,
                        thread_id=str(thread.id),
                        reference=reference,
                    )
                    for kind, reference in references.items()
                }
                workspace_archive = restored_archives["workspace"]
                native_archive = restored_archives["native_session"]
                monkeypatch.chdir(tmp_path)
                shutil.rmtree(workspace)
                restore(workspace_archive, workspace)
                files = unpack_session(
                    native_archive,
                    harness="opencode",
                    harness_version="1.18.29",
                    session_id=native_id,
                    thread_id=str(thread.id),
                )
                assert files is not None
                restore_session(files, session_home / "data" / "opencode")
                ctx["flow_name"] = "Edited conversation flow"
                if with_repository:
                    ctx["git_clone_config"]["target_branch"] = "edited-target"
            clone = agent._prepare_git_clone_command(ctx)
            shell = (
                agent._wrap_clone_with_workspace_restore(clone, ctx)
                if clone
                else "true"
            )
            result = run_shell(shell, workspace, tmp_path)
            assert result.returncode == 0, result.stdout + result.stderr
            if with_repository:
                branch = branch or git(workspace, "branch", "--show-current")
                head = head or git(workspace, "rev-parse", "HEAD")
                assert git(workspace, "branch", "--show-current") == branch
                assert git(workspace, "rev-parse", "HEAD") == head
                assert (workspace / "answer.txt").read_text() == "amber\n"
                if turn > 1:
                    assert (workspace / "notes.txt").read_text() == "saved scratch work"
                (workspace / "notes.txt").write_text("saved scratch work")
            prompt = tmp_path / "prompt.txt"
            message = crud.messages(db_session, thread.id, after=turn - 1)[0].text
            ctx["trigger_event_data"]["_conversation_message"] = message
            orchestrator = FlowExecutionOrchestrator(
                db_session, thread.flow_id, ctx["trigger_event_data"], MagicMock()
            )
            orchestrator.flow = crud_flow.get(db_session, id=thread.flow_id)
            orchestrator.execution_log = execution
            resolved_prompt = asyncio.run(orchestrator._resolve_prompt())
            if turn > 1:
                assert resolved_prompt == message
                assert thread.idle_timeout_seconds == 1
                assert thread.max_turns == 30
            prompt.write_text(resolved_prompt)
            monkeypatch.chdir(workspace)
            monkeypatch.setenv("PRELOOP_CONVERSATION_TURN", str(turn))
            monkeypatch.setattr(
                sys, "argv", ["driver", "preloop/fixture", str(prompt), native_id]
            )
            started = time.monotonic()
            result = driver.main()
            if not with_repository and turn == 1 and result == 1:
                # The pinned attach client can exit without forwarding any
                # completion events for a session in a non-Git directory.
                # Keep this neighbouring transport limitation visible.
                assert len(primary_requests) == 1
                assert thread.state == "failed"
                assert crud.messages(db_session, thread.id)[0].status == "failed"
                pytest.xfail("OpenCode 1.18.29 attach omits non-Git completion events")
            assert result == 0
            assert thread.turns == turn
            assert thread.stop_reason == "idle_timeout"
            messages = crud.messages(db_session, thread.id)
            assert messages[-1].answer == f"amber; user turns={turn}"
            assert messages[-1].status == "completed"
            restored_id = session_marker.read_text()
            assert not native_id or native_id == restored_id
            native_id = restored_id
            native_archive = pack_session(
                select_session_files(
                    session_home / "data" / "opencode", "opencode", native_id
                ),
                harness="opencode",
                harness_version="1.18.29",
                session_id=native_id,
                thread_id=str(thread.id),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            workspace_archive = capture(workspace, max_bytes=2_000_000)
            for kind, archive in (
                ("workspace", workspace_archive),
                ("native_session", native_archive),
            ):
                reference = flow_artifacts.put_artifact(
                    db_session,
                    account_id=thread.account_id,
                    flow_id=thread.flow_id,
                    execution_id=execution.id,
                    thread_id=str(thread.id),
                    kind=kind,
                    archive=archive,
                )
                references[kind] = reference
                if kind == "native_session":
                    crud_flow_execution.update(
                        db_session,
                        db_obj=execution,
                        obj_in={
                            "cli_session": {
                                "agent_type": "opencode",
                                "session_id": native_id,
                                "artifact_reference": reference.model_dump(mode="json"),
                            }
                        },
                    )
            crud_flow_execution.update(
                db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
            )
            service.reconcile(db_session, thread)
            assert thread.state == "stopped"
            if turn == 1:
                flow = crud_flow.get(db_session, id=thread.flow_id)
                crud_flow.update(
                    db_session,
                    db_obj=flow,
                    account_id=thread.account_id,
                    flow_in=FlowUpdate(
                        **{
                            "prompt_template": "Edited prompt must not be replayed",
                            "agent_config": {
                                "interactive": {
                                    "enabled": True,
                                    "max_turns": 2,
                                    "idle_timeout_seconds": 100,
                                }
                            },
                        }
                    ),
                )
                (source / "answer.txt").write_text("blue\n")
                git(source, "commit", "-am", "Move remote main")
                git(source, "push", "origin", "main")
        assert len(primary_requests) == 3
        assert "Remember the code word amber" in json.dumps(
            primary_requests[-1]["messages"]
        )
        assert [item.turn for item in crud.messages(db_session, thread.id)] == [1, 2, 3]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
