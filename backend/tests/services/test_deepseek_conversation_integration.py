"""Opt-in real dsh, local PostgreSQL, HTTP gateway and native checkpoint proof.

Run with DEEPSEEK_CONVERSATION_SMOKE=1 and pinned dsh on PATH. The upstream is
synthetic; no provider credentials are consumed. Runtime lifetimes are fresh
processes with fresh homes and a real repository clone at the same workspace path.
"""

from __future__ import annotations

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
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.agents import deepseek_conversation as driver
from preloop.agents.checkpoint_client import capture, restore
from preloop.agents.harness import DeepSeekAgent
from preloop.agents.session_manifest import (
    pack_session,
    restore_session,
    select_session_files,
    unpack_session,
)
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.crud import flow_conversation as crud
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.flow import FlowUpdate
from preloop.services import flow_artifacts, flow_conversations as service
from tests.agents.test_conversation_workspace_branch import context, git, run_shell
from tests.agents.test_conversation_workspace_branch import repository as repository
from tests.integration.test_gateway_e2e import _mint_runtime_token, _seed_gateway_model
from tests.services.test_flow_conversations import thread as thread


@pytest.mark.skipif(
    os.environ.get("DEEPSEEK_CONVERSATION_SMOKE") != "1",
    reason="opt-in pinned DeepSeek conversation integration",
)
def test_three_live_turns_idle_and_resume_through_gateway(
    db_session: Session,
    test_user: models.User,
    thread: models.FlowConversation,  # noqa: F811
    repository: tuple[Path, Path],  # noqa: F811
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native history, Git state, durable answers and owner metering survive stop."""
    binary = shutil.which("dsh")
    docker_image = os.environ.get("DEEPSEEK_CONVERSATION_IMAGE")
    assert binary
    assert (
        subprocess.check_output([binary, "--version"], text=True).strip()
        == "0.1.5-rc.2"
    )
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    monkeypatch.setattr(settings, "model_endpoint_allowed_cidrs", "127.0.0.0/8")
    lock = threading.RLock()
    requests: list[dict[str, Any]] = []

    class Upstream(BaseHTTPRequestHandler):
        """Report history actually sent by the native agent, with metered tokens."""

        def log_message(self, format: str, *args: Any) -> None:
            """Keep fixture requests out of logs."""

        def do_POST(self) -> None:  # noqa: N802
            """Serve the OpenAI streaming wire protocol over a real socket."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            messages = body["messages"]
            humans = [
                m
                for m in messages
                if m["role"] == "user"
                and not str(m["content"]).startswith("Current runtime context.")
            ]
            answer = (
                "amber"
                if "Remember the code word amber" in json.dumps(messages)
                else "missing"
            )
            answer += f"; turns={len(humans)}"
            self.send_response(200)
            if not body.get("stream"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "chatcmpl_synthetic",
                            "object": "chat.completion",
                            "created": 1,
                            "model": "gpt-4o-mini",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": answer,
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 20,
                                "completion_tokens": 5,
                                "total_tokens": 25,
                            },
                        }
                    ).encode()
                )
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": answer}, None),
                ({}, "stop"),
            ]:
                event = {
                    "id": "chatcmpl_synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if finish:
                    event["usage"] = {
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                    }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    class Gateway(BaseHTTPRequestHandler):
        """Expose the real ASGI gateway and runtime endpoints to the native CLI."""

        def log_message(self, format: str, *args: Any) -> None:
            """Never log authorization or request payloads."""

        def exchange(self) -> None:
            """Relay without altering credentials, model messages or runtime state."""
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                response = client.request(
                    self.command,
                    self.path,
                    content=body,
                    headers={
                        key: value
                        for key, value in self.headers.items()
                        if key.lower()
                        in {"authorization", "content-type", "x-preloop-session-id"}
                    },
                )
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", response.headers.get("Content-Type", "application/json")
            )
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = exchange  # noqa: N815
        do_POST = exchange  # noqa: N815

    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), Upstream),
        ThreadingHTTPServer(("0.0.0.0" if docker_image else "127.0.0.1", 0), Gateway),
    ]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    host = "host.docker.internal" if docker_image else "127.0.0.1"
    endpoint = f"http://{host}:{servers[1].server_port}"
    monkeypatch.setattr(settings, "preloop_url", endpoint)
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="synthetic",
        api_endpoint=f"http://127.0.0.1:{servers[0].server_port}/v1",
    )
    flow = crud_flow.get(db_session, id=thread.flow_id)
    crud_flow.update(
        db_session,
        db_obj=flow,
        account_id=thread.account_id,
        flow_in=FlowUpdate(agent_type="deepseek"),
    )
    crud.save(db_session, thread, idle_timeout_seconds=2)
    thread_id, account_id, flow_id = thread.id, thread.account_id, thread.flow_id
    owner_id = test_user.id
    agent = DeepSeekAgent({})
    workspace = tmp_path / "workspace"
    upstream, source = repository
    repo_root = Path(__file__).resolve().parents[3]
    plugin = repo_root / "runtime-plugins/harness-preloop"
    native_id = ""
    archives: dict[str, Any] = {}
    references: dict[str, Any] = {}
    measurements: list[dict[str, float | int]] = []
    first_tokens: dict[int, float] = {}
    process = None
    containers: list[str] = []
    branch = head = None
    try:
        for lifetime, first_turn in enumerate((1, 4)):
            with lock:
                row = crud.get(db_session, thread_id, account_id)
                previous = row.execution_id
                if lifetime:
                    service.resume_checkpoint(db_session, row)
                    crud.admit(
                        db_session,
                        row,
                        text="Recall the first turn.",
                        request_id=uuid4(),
                        starting=True,
                    )
                    execution = crud_flow_execution.create(
                        db_session,
                        FlowExecutionCreate(flow_id=flow_id, status="RUNNING"),
                    )
                    crud.bind_execution(db_session, row, execution.id)
                execution_id = row.execution_id
                ctx = context(upstream, str(execution_id))
                ctx.update(
                    account_id=str(account_id),
                    flow_id=str(flow_id),
                    model_gateway_enabled=True,
                )
                ctx["trigger_event_data"].update(
                    _conversation_id=str(thread_id), _session_thread_id=str(thread_id)
                )
                home = tmp_path / f"runtime-{lifetime}"
                if lifetime:
                    ctx["trigger_event_data"]["_resume"] = {
                        "execution_id": str(previous)
                    }
                    assert (
                        service.bound_resume(db_session, ctx)["session_id"] == native_id
                    )
                    ctx["checkpoint_env"] = {
                        "PRELOOP_CHECKPOINT_GET_TOKEN": "synthetic"
                    }
                    shutil.rmtree(workspace)
                    for kind, reference in references.items():
                        archives[kind] = flow_artifacts.get_artifact(
                            db_session,
                            account_id=account_id,
                            flow_id=flow_id,
                            thread_id=str(thread_id),
                            reference=reference,
                        )
                    restore(archives["workspace"], workspace)
                    files = unpack_session(
                        archives["native_session"],
                        harness="deepseek",
                        harness_version="0.1.5-rc.2",
                        session_id=native_id,
                        thread_id=str(thread_id),
                    )
                    assert files
                    restore_session(files, home / "sessions")
                key, token = _mint_runtime_token(
                    db_session,
                    test_user,
                    principal={
                        "type": "flow_execution",
                        "id": str(execution_id),
                        "name": "Conversation",
                    },
                    context_extra={
                        "flow_execution_id": str(execution_id),
                        "flow_id": str(flow_id),
                    },
                )
                runtime_env = service.runtime_environment(db_session, ctx)
            clone = agent._prepare_git_clone_command(ctx)
            result = run_shell(
                agent._wrap_clone_with_workspace_restore(clone, ctx),
                workspace,
                tmp_path,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            branch = branch or git(workspace, "branch", "--show-current")
            head = head or git(workspace, "rev-parse", "HEAD")
            assert git(workspace, "branch", "--show-current") == branch
            assert git(workspace, "rev-parse", "HEAD") == head
            assert (workspace / "answer.txt").read_text() == "amber\n"
            if lifetime:
                assert (workspace / "notes.txt").read_text() == "saved work"
            (workspace / "notes.txt").write_text("saved work")
            env = {
                **os.environ,
                **runtime_env,
                **asyncio.run(
                    agent._prepare_environment(
                        {
                            "model_gateway_enabled": True,
                            "model_gateway_model_alias": "synthetic",
                            "model_gateway_url": endpoint + "/openai/v1",
                            "model_gateway_token": token,
                        }
                    )
                ),
            }
            env.update(DSH_HOME=str(home), HOME=str(home), PRELOOP_API_URL=endpoint)
            subprocess.run(["node", str(plugin / "bootstrap.mjs")], env=env, check=True)
            patch_file = home / "cordis.patch.yml"
            composition = json.loads(patch_file.read_text())
            composition.extend(
                [
                    {"id": "headless-runner", "disabled": True},
                    {"id": "preloop-headless-startup", "disabled": True},
                    {
                        "id": "session-persistence-jsonl",
                        "config": {
                            "root": str(home / "sessions"),
                            "compression": "none",
                        },
                    },
                    {
                        "insert": [
                            {
                                "id": "conversation",
                                "name": str(Path(driver.__file__).with_suffix(".mjs")),
                            }
                        ]
                    },
                ]
            )
            if docker_image:
                for entry in composition:
                    if entry.get("id") == "session-persistence-jsonl":
                        entry["config"]["root"] = "/tmp/preloop-dsh/sessions"
                    for inserted in entry.get("insert", []):
                        inserted["name"] = (
                            inserted["name"]
                            .replace(str(plugin), "/opt/preloop-harness")
                            .replace(
                                str(Path(driver.__file__).parent), "/opt/preloop-agents"
                            )
                        )
            patch_file.write_text(json.dumps(composition))
            prompt = tmp_path / "prompt.txt"
            prompt.write_text(
                "Remember the code word amber."
                if not lifetime
                else "Recall the first turn."
            )
            started = time.monotonic()
            command = [
                sys.executable,
                str(Path(driver.__file__)),
                str(prompt),
                native_id,
            ]
            if docker_image:
                home.chmod(0o777)
                workspace.chmod(0o777)
                for path in home.rglob("*"):
                    path.chmod(0o777 if path.is_dir() else 0o666)
                env.update(DSH_HOME="/tmp/preloop-dsh", HOME="/tmp/preloop-dsh")
                container_name = "preloop-dsh-smoke-" + uuid4().hex
                containers.append(container_name)
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    container_name,
                    "--user",
                    "10000:10000",
                    "--workdir",
                    "/workspace",
                    "--mount",
                    f"type=bind,src={workspace},dst=/workspace",
                    "--mount",
                    f"type=bind,src={home},dst=/tmp/preloop-dsh",
                    "--mount",
                    f"type=bind,src={prompt},dst=/tmp/prompt.txt,readonly",
                ]
                for name in env:
                    if name.startswith(("PRELOOP_", "DSH_")) or name == "HOME":
                        command.extend(["--env", name])
                command.extend(
                    [
                        docker_image,
                        "python3",
                        "/opt/preloop-agents/deepseek_conversation.py",
                        "/tmp/prompt.txt",
                        native_id,
                    ]
                )
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output: list[str] = []

            def collect(child: subprocess.Popen[str], lines: list[str]) -> None:
                """Timestamp the first text delta observed by the supervisor."""
                assert child.stdout is not None
                for line in child.stdout:
                    lines.append(line)
                    if line.startswith("PRELOOP_CONVERSATION_FIRST_TOKEN "):
                        first_tokens[int(line.split()[-1])] = time.monotonic()

            reader = threading.Thread(
                target=collect, args=(process, output), daemon=True
            )
            reader.start()
            for turn in range(first_turn, 4 if not lifetime else 5):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    with lock:
                        row = crud.get(db_session, thread_id, account_id)
                        db_session.refresh(row)
                        if row.state == "waiting":
                            message = crud.messages(db_session, thread_id)[-1]
                            assert message.answer == f"amber; turns={turn}"
                            measurements.append(
                                {
                                    "turn": turn,
                                    "answer_ms": (time.monotonic() - started) * 1000,
                                    "first_token_ms": (first_tokens[turn] - started)
                                    * 1000,
                                }
                            )
                            break
                    assert process.poll() is None, "".join(output)
                    time.sleep(0.05)
                else:
                    pytest.fail("Conversation did not produce a durable answer")
                if turn < 3:
                    with lock:
                        assert service.admission_notice(row) is None
                        crud.admit(
                            db_session,
                            row,
                            text=f"Continue turn {turn + 1}.",
                            request_id=uuid4(),
                            starting=False,
                        )
                    started = time.monotonic()
            process.wait(timeout=40)
            reader.join(timeout=5)
            stdout = "".join(output)
            assert process.stderr is not None
            stderr = process.stderr.read()
            assert process.returncode == 0, stdout + stderr
            assert "FLOW_EXECUTION_SUCCESS" in stdout
            restored_id = next(
                line.split()[-1]
                for line in stdout.splitlines()
                if line.startswith("PRELOOP_AGENT_SESSION")
            )
            assert not native_id or restored_id == native_id
            native_id = restored_id
            archives["native_session"] = pack_session(
                select_session_files(home / "sessions", "deepseek", native_id),
                harness="deepseek",
                harness_version="0.1.5-rc.2",
                session_id=native_id,
                thread_id=str(thread_id),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            archives["workspace"] = capture(workspace, max_bytes=2_000_000)
            with lock:
                execution = crud_flow_execution.get(db_session, id=execution_id)
                for kind, archive in archives.items():
                    references[kind] = flow_artifacts.put_artifact(
                        db_session,
                        account_id=account_id,
                        flow_id=flow_id,
                        execution_id=execution_id,
                        thread_id=str(thread_id),
                        kind=kind,
                        archive=archive,
                    )
                crud_flow_execution.update(
                    db_session,
                    db_obj=execution,
                    obj_in={
                        "status": "SUCCEEDED",
                        "cli_session": {
                            "agent_type": "deepseek",
                            "session_id": native_id,
                            "artifact_reference": references[
                                "native_session"
                            ].model_dump(mode="json"),
                        },
                    },
                )
                row = crud.get(db_session, thread_id, account_id)
                service.reconcile(db_session, row)
                assert row.state == "stopped"
            if not lifetime:
                (source / "answer.txt").write_text("remote moved\n")
                git(source, "commit", "-am", "Move source")
                git(source, "push", "origin", "main")
        primary = [
            r
            for r in requests
            if any(
                m.get("content") == "Remember the code word amber."
                for m in r["messages"]
            )
        ]
        assert len(primary) == 4
        assert (
            len({m["content"] for m in primary[-1]["messages"] if m["role"] == "user"})
            >= 4
        )
        with lock:
            usage = crud_api_usage.list_gateway_rows_in_window(
                db_session,
                account_id=account_id,
                start=datetime.now(UTC) - timedelta(hours=1),
                end=datetime.now(UTC) + timedelta(hours=1),
            )
            assert len(usage) >= 4
            assert all(
                item.user_id == owner_id and item.total_tokens == 25 for item in usage
            )
            assert len({item.flow_execution_id for item in usage}) == 2
        print(
            "DeepSeek synthetic local gateway measurements: " + json.dumps(measurements)
        )
    finally:
        if process and process.poll() is None:
            process.kill()
            process.communicate()
        for container_name in containers:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        for server in servers:
            server.shutdown()
            server.server_close()
