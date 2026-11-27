"""Bounded, pull-driven model transport over an authenticated runner socket."""

from __future__ import annotations

import asyncio
import json
import hmac
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import nats
from nats.aio.msg import Msg
from fastapi import WebSocket
from litellm import ModelResponse
from litellm.types.utils import ModelResponseStream

from preloop.config import settings
from preloop.services.model_gateway_errors import ModelGatewayAPIError

MAX_REQUEST_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_REQUESTS = 4


def timeout_message(kind: str, seconds: float) -> str:
    """Describe a timeout without exposing an upstream response body."""
    value = f"{seconds:g}"
    if kind == "silent":
        return (
            f"local model sent no data for {value} s "
            f"(RUNNER_MODEL_SILENT_SECONDS={value})"
        )
    if kind == "idle":
        return (
            f"runner sent no frames for {value} s (RUNNER_MODEL_IDLE_SECONDS={value})"
        )
    return f"model request exceeded {value} s (effective RUNNER_MODEL_TOTAL_SECONDS)"


def runner_model_flow_budget(db: Any, auth_context: Any) -> float | None:
    """Read the calling flow's remaining budget before releasing its DB session.

    Args:
        db: Gateway's current database session.
        auth_context: Authenticated credential context, never request JSON.

    Returns:
        Remaining seconds, or None for a caller outside a flow execution.

    Raises:
        ModelGatewayAPIError: The bound execution is absent or already terminal.
    """
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.services.approval_park import consumed_seconds_from_details
    from preloop.services.flow_execution_limits import TERMINAL_STATUSES

    key = getattr(auth_context, "api_key", None)
    context = getattr(key, "context_data", None)
    execution_id = (
        context.get("flow_execution_id") if isinstance(context, dict) else None
    )
    if not execution_id:
        return None
    execution = crud_flow_execution.get(
        db, id=execution_id, account_id=str(auth_context.account_id)
    )
    flow = crud_flow.get(db, id=execution.flow_id) if execution else None
    if (
        not flow
        or str(flow.account_id) != str(auth_context.account_id)
        or execution.status in TERMINAL_STATUSES
    ):
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=504,
            code="runner_proxy_timeout",
            message="Flow execution is no longer active; model request cancelled",
        )
    # Match the orchestrator's bounds and park/conversation accounting.
    seconds = max(
        60, min(86400, flow.timeout_seconds or settings.flow_execution_max_wait_seconds)
    )
    details = execution.trigger_event_details or {}
    consumed = consumed_seconds_from_details(details)
    if details.get("_conversation_id"):
        from uuid import UUID
        from preloop.services.flow_conversations import consumed_seconds

        consumed = consumed_seconds(db, UUID(details["_conversation_id"]), execution.id)
    if consumed:
        seconds = max(0 if details.get("_conversation_id") else 60, seconds - consumed)
    anchor = execution.launch_requested_at or execution.start_time
    if anchor is None:
        return 0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0, seconds - (datetime.now(timezone.utc) - anchor).total_seconds())


@dataclass
class ActiveRequest:
    """Per-request deadlines; keepalives update liveness only."""

    deadline: float
    last_frame: float
    last_data: float
    idle: float
    silent: float
    expiry: asyncio.TimerHandle | None = None


def normalize_local_base_url(value: str) -> str:
    """Accept only explicit HTTP loopback OpenAI-compatible base URLs."""
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "http"
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and url.port is not None
            and 0 < url.port <= 65535
            and url.username is None
            and url.password is None
            and url.path in {"/v1", "/v1/"}
            and not url.query
            and not url.fragment
            and "?" not in value
            and "#" not in value
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Runner endpoint must be http://loopback:port/v1")
    return value.rstrip("/")


def runner_model_subject(account_id: str, runner_id: str) -> str:
    """Build an internal subject from trusted database UUIDs."""
    return f"preloop.runner_models.{account_id}.{runner_id}"


class RunnerModelConnection:
    """Bridge one socket to internal NATS RPC with one frame in flight per call."""

    def __init__(
        self,
        websocket: WebSocket,
        account_id: str,
        runner_id: str,
        *,
        credential_hash: str | None = None,
        connection_id: str | None = None,
    ) -> None:
        self.websocket = websocket
        self.credential_hash = credential_hash
        self.connection_id = connection_id
        self.account_id = account_id
        self.runner_id = runner_id
        self.subject = runner_model_subject(account_id, runner_id)
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.messages: dict[str, Msg] = {}
        self.closed = False
        self.active: dict[str, ActiveRequest] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.nc: Any = None
        self.subscription: Any = None

    async def start(self) -> None:
        """Subscribe only while this runner has an authenticated live socket."""
        self.nc = await nats.connect(
            settings.nats_url,
            connect_timeout=2,
            max_reconnect_attempts=0,
            allow_reconnect=False,
        )

        async def receive(message: Msg) -> None:
            task = asyncio.create_task(self._request(message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        self.subscription = await self.nc.subscribe(self.subject, cb=receive)
        await self.nc.flush()

    async def _request(self, message: Msg) -> None:
        request_id = ""
        owned = False
        try:
            raw = json.loads(message.data)
            request_id = str(raw["request_id"])
            operation = raw.get("operation")
            if self.closed:
                await message.respond(
                    b'{"status":503,"error":"runner_offline","done":true}'
                )
                return
            now = time.monotonic()
            for key, state in list(self.active.items()):
                if now >= state.deadline:
                    await self._abort(key, "total")
            if operation == "cancel":
                await self._abort(request_id)
                if message.reply:
                    await message.respond(b'{"done":true}')
                return
            if request_id in self.messages:
                await message.respond(
                    b'{"status":409,"error":"runner_proxy_error","done":true}'
                )
                return
            self.messages[request_id] = message
            owned = True
            if operation == "start":
                if len(self.active) >= MAX_ACTIVE_REQUESTS:
                    await message.respond(
                        b'{"status":429,"error":"runner_busy","done":true}'
                    )
                    return
                if len(message.data) > MAX_REQUEST_BYTES:
                    raise ValueError("request too large")
                normalize_local_base_url(raw["base_url"])
                # Reserve before yielding to the database worker, so concurrent
                # authorizations cannot all observe the same available slot.
                limits = raw.get("timeouts") or {}
                total = min(
                    settings.runner_model_total_seconds,
                    float(
                        limits.get("total_seconds", settings.runner_model_total_seconds)
                    ),
                )
                if not 0 < total <= settings.runner_model_total_seconds:
                    raise ValueError("invalid total timeout")
                state = ActiveRequest(
                    deadline=now + total,
                    last_frame=now,
                    last_data=now,
                    idle=settings.runner_model_idle_seconds,
                    silent=settings.runner_model_silent_seconds,
                )
                self.active[request_id] = state
                state.expiry = asyncio.get_running_loop().call_later(
                    total, self._schedule_expiry, request_id
                )
                raw["timeouts"] = {
                    "idle_seconds": state.idle,
                    "silent_seconds": state.silent,
                    "total_seconds": total,
                }
                if not await asyncio.to_thread(self._authorized, raw):
                    raise ValueError("unregistered model")
            elif operation != "next" or request_id not in self.active:
                raise ValueError("unknown request")
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            state = self.active.get(request_id)
            if state is None or time.monotonic() >= state.deadline:
                await self._abort(request_id, "total")
                await message.respond(json.dumps(self._timeout_reply("total")).encode())
                return
            # Consumer back-pressure is charged to total, not model silence.
            state.last_frame = state.last_data = time.monotonic()
            if operation == "start":
                raw["timeouts"]["total_seconds"] = state.deadline - time.monotonic()
            await self.websocket.send_json(raw)
            while not future.done():
                now = time.monotonic()
                kind, deadline = min(
                    (
                        ("total", state.deadline),
                        ("idle", state.last_frame + state.idle),
                        ("silent", state.last_data + state.silent),
                    ),
                    key=lambda item: item[1],
                )
                if deadline <= now:
                    await self._abort(request_id, kind)
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(future), deadline - now)
                except TimeoutError:
                    # A keepalive may have moved the idle deadline while waiting.
                    continue
            reply = future.result()
            if reply.get("done") or reply.get("error"):
                await self._abort(request_id)
            await message.respond(json.dumps(reply).encode())
        except asyncio.CancelledError:
            await self._abort(request_id)
            raise
        except (ValueError, KeyError, TypeError):
            await self._abort(request_id)
            await message.respond(
                b'{"status":403,"error":"runner_model_not_allowed","done":true}'
            )
        except Exception:
            await self._abort(request_id)
            await message.respond(
                b'{"status":503,"error":"runner_offline","done":true}'
            )
        finally:
            if owned:
                self.pending.pop(request_id, None)
                self.messages.pop(request_id, None)

    @staticmethod
    def _timeout_reply(kind: str) -> dict[str, Any]:
        """Keep the timeout error code stable across protocol versions."""
        return {
            "status": 504,
            "error": "runner_proxy_timeout",
            "timeout": kind,
            "done": True,
        }

    def _schedule_expiry(self, request_id: str) -> None:
        """Expire even a request whose consumer never pulls another frame."""
        task = asyncio.create_task(self._abort(request_id, "total"))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _abort(self, request_id: str, kind: str | None = None) -> None:
        """Release a slot, wake a pending RPC and stop the host's local request."""
        state = self.active.pop(request_id, None)
        if state is not None and state.expiry is not None:
            state.expiry.cancel()
        future = self.pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(self._timeout_reply(kind) if kind else {"done": True})
        if state is not None:
            try:
                await asyncio.wait_for(
                    self.websocket.send_json(
                        {
                            "type": "model_request",
                            "operation": "cancel",
                            "request_id": request_id,
                        }
                    ),
                    timeout=1,
                )
            except Exception:
                pass  # Disconnect already cancels all local calls.

    def _authorized(self, raw: dict[str, Any]) -> bool:
        """Recheck stored model/account binding before handing work to the host."""
        from preloop.models.crud import crud_ai_model
        from preloop.models.crud.flow_runner import crud_flow_runner
        from preloop.models.db.session import get_db_session

        for db in get_db_session():
            runner = crud_flow_runner.get(
                db, id=self.runner_id, account_id=self.account_id
            )
            if (
                not runner
                or not self.credential_hash
                or not self.connection_id
                or not hmac.compare_digest(
                    runner.token_hash or "", self.credential_hash
                )
                or (runner.publication_capabilities or {}).get("connection_id")
                != self.connection_id
            ):
                raise ConnectionError("Runner connection is no longer current")
            model = crud_ai_model.get(
                db, id=raw.get("model_id"), account_id=self.account_id
            )
            return bool(
                model
                and model.endpoint_type == "runner"
                and str(model.runner_id) == self.runner_id
                and model.api_endpoint == raw.get("base_url")
                and model.model_identifier == (raw.get("body") or {}).get("model")
            )
        return False

    def response(self, raw: dict[str, Any]) -> None:
        """Accept a reply only on the socket that owns its pending request."""
        future = self.pending.get(str(raw.get("request_id")))
        if future is None or future.done():
            return
        if (
            type(raw.get("status", 200)) is not int
            or not 200 <= raw.get("status", 200) <= 599
            or not isinstance(raw.get("data", ""), str)
            or type(raw.get("done", False)) is not bool
            or type(raw.get("keepalive", False)) is not bool
            or type(raw.get("model_activity", False)) is not bool
            or (
                raw.get("timeout") is not None
                and raw["timeout"] not in ("idle", "silent", "total")
            )
            or (
                raw.get("error") is not None
                and (not isinstance(raw["error"], str) or len(raw["error"]) > 128)
            )
        ):
            future.set_result(
                {"status": 502, "error": "runner_proxy_error", "done": True}
            )
        elif len(json.dumps(raw).encode()) > MAX_FRAME_BYTES + 2048:
            future.set_result(
                {"status": 502, "error": "runner_frame_too_large", "done": True}
            )
        else:
            state = self.active.get(str(raw.get("request_id")))
            if state is not None:
                state.last_frame = time.monotonic()
                if raw.get("model_activity") or raw.get("data"):
                    state.last_data = state.last_frame
            if not raw.get("keepalive"):
                future.set_result(raw)

    async def close(self) -> None:
        """Fail RPCs and cancel authorizations before closing the subscription."""
        if self.closed:
            return
        self.closed = True
        for task in self.tasks:
            task.cancel()
        for future in self.pending.values():
            if not future.done():
                future.set_result(
                    {"status": 503, "error": "runner_offline", "done": True}
                )
        for message in list(self.messages.values()):
            try:
                await message.respond(
                    b'{"status":503,"error":"runner_offline","done":true}'
                )
            except Exception:
                pass
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for state in self.active.values():
            if state.expiry is not None:
                state.expiry.cancel()
        self.active.clear()
        if self.nc is not None:
            await self.nc.close()


class RunnerModelStream:
    """Synchronous gateway iterator with a private NATS I/O event loop."""

    def __init__(
        self, model: Any, body: dict[str, Any], *, total_seconds: float | None = None
    ) -> None:
        self.total_seconds = min(
            settings.runner_model_total_seconds,
            total_seconds
            if total_seconds is not None
            else settings.runner_model_total_seconds,
        )
        self.timeouts = {
            "idle_seconds": settings.runner_model_idle_seconds,
            "silent_seconds": settings.runner_model_silent_seconds,
            "total_seconds": self.total_seconds,
        }
        self.subject = runner_model_subject(str(model.account_id), str(model.runner_id))
        self.request_id = str(uuid4())
        self.start_frame = {
            "type": "model_request",
            "operation": "start",
            "request_id": self.request_id,
            "model_id": str(model.id),
            "base_url": model.api_endpoint,
            "body": body,
            "timeouts": self.timeouts.copy(),
        }
        if len(json.dumps(self.start_frame).encode()) > MAX_REQUEST_BYTES:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=413,
                message="Runner model request exceeds 1 MiB",
            )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread_started = False
        self.nc: Any = None
        self.started = time.monotonic()
        self.total_bytes = 0
        self.closed = False
        self.first = True

    async def _rpc(self, frame: dict[str, Any]) -> dict[str, Any]:
        if self.nc is None:
            self.nc = await nats.connect(
                settings.nats_url,
                connect_timeout=2,
                max_reconnect_attempts=0,
                allow_reconnect=False,
            )
        reply = await self.nc.request(
            self.subject,
            json.dumps(frame).encode(),
            timeout=max(0.001, self.total_seconds - (time.monotonic() - self.started))
            + 2,
        )
        return json.loads(reply.data)

    def read(self) -> dict[str, Any]:
        """Pull one bounded frame, mapping broker failures to gateway errors."""
        remaining = self.total_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            self.close()
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=504,
                code="runner_proxy_timeout",
                message=timeout_message("total", self.total_seconds),
            )
        frame = (
            self.start_frame
            if self.first
            else {
                "type": "model_request",
                "operation": "next",
                "request_id": self.request_id,
            }
        )
        if self.first:
            frame["timeouts"] = {**self.timeouts, "total_seconds": remaining}
        self.first = False
        if not self.thread_started:
            self.thread.start()
            self.thread_started = True
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(self._rpc(frame), self.loop)
            reply = future.result(timeout=remaining + 5)
        except Exception as exc:
            if future is not None:
                future.cancel()
            self.close()
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=503,
                code="runner_offline",
                message="Runner is offline or unavailable",
            ) from exc
        self.total_bytes += len(json.dumps(reply).encode())
        if self.total_bytes > MAX_RESPONSE_BYTES:
            self.close()
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                message="Runner model response exceeds limit",
            )
        status = reply.get("status", 200)
        if type(status) is not int or not 200 <= status <= 599:
            status = 502
        if reply.get("error") or status >= 300:
            code = reply.get("error")
            if code not in {
                "runner_offline",
                "runner_busy",
                "runner_model_not_allowed",
                "runner_proxy_timeout",
                "runner_frame_too_large",
                "runner_response_limit",
                "runner_upstream_unavailable",
                "runner_upstream_read_error",
            }:
                code = "runner_upstream_error"
            self.close()
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=status if status >= 400 else 502,
                code=code,
                message=timeout_message(
                    reply["timeout"], self.timeouts[reply["timeout"] + "_seconds"]
                )
                if reply.get("timeout") in {"idle", "silent", "total"}
                else (
                    "Runner is offline or unavailable"
                    if code == "runner_offline"
                    else "Runner model request failed: " + code
                ),
            )
        return reply

    def __iter__(self) -> RunnerModelStream:
        """Return this closeable, lazy stream without allocating a worker."""
        return self

    def __next__(self) -> ModelResponseStream:
        """Parse one OpenAI SSE event without accumulating the generation."""
        try:
            while not self.closed:
                reply = self.read()
                line = reply.get("data", "")
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        return ModelResponseStream(**json.loads(data))
                    except (ValueError, TypeError) as exc:
                        raise ModelGatewayAPIError(
                            provider="openai",
                            status_code=502,
                            message="Runner returned an invalid model response",
                        ) from exc
                if reply.get("done"):
                    break
        except BaseException:
            self.close()
            raise
        self.close()
        raise StopIteration

    def close(self) -> None:
        """Cancel upstream work and shut down this call's private I/O thread."""
        if self.closed:
            return
        self.closed = True
        if not self.thread_started:
            self.loop.close()
            return

        async def cleanup() -> None:
            if self.nc is not None:
                try:
                    await self.nc.publish(
                        self.subject,
                        json.dumps(
                            {
                                "type": "model_request",
                                "operation": "cancel",
                                "request_id": self.request_id,
                            }
                        ).encode(),
                    )
                    await self.nc.flush(timeout=1)
                finally:
                    await self.nc.close()

        try:
            asyncio.run_coroutine_threadsafe(cleanup(), self.loop).result(timeout=3)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
            if not self.thread.is_alive():
                self.loop.close()


def runner_completion(
    model: Any,
    body: dict[str, Any],
    *,
    stream: bool,
    total_seconds: float | None = None,
) -> Any:
    """Dispatch only chat completion provider I/O through the runner tunnel."""
    tunnel = RunnerModelStream(model, body, total_seconds=total_seconds)
    if stream:
        return tunnel
    try:
        reply = tunnel.read()
        try:
            return ModelResponse(**json.loads(reply["data"]))
        except (ValueError, TypeError) as exc:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                message="Runner returned an invalid model response",
            ) from exc
    finally:
        tunnel.close()
