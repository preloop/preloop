"""Browser Use adapter: conversion, batching, retries and outage behaviour."""

from __future__ import annotations

from typing import Any

import asyncio
import base64
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pydantic
import pytest

from preloop_browser_use import (
    PreloopBrowserUseReporter,
    PreloopTarget,
    StepPoster,
    history_item_to_step,
    history_to_steps,
)

FIXTURE = Path(__file__).parent / "fixtures" / "browser_use_history_12.json"
TARGET = PreloopTarget("http://preloop.test", "agent-key", "sess-1")
STEPS_URL = "http://preloop.test/api/v1/runtime-sessions/sess-1/browser-steps"


def _history() -> dict:
    return json.loads(FIXTURE.read_text())


def _as_objects(value):
    """Turn the dict dump into attribute objects like live pydantic models.

    ``attributes`` stays a dict, as on Browser Use's ``DOMHistoryElement``.
    """
    if isinstance(value, dict) and not _is_action(value):
        return SimpleNamespace(
            **{k: v if k == "attributes" else _as_objects(v) for k, v in value.items()}
        )
    if isinstance(value, list):
        return [_as_objects(v) for v in value]
    return value


def _is_action(value: dict) -> bool:
    return (
        len(value) == 1
        and isinstance(next(iter(value.values())), dict)
        and (
            "url" in next(iter(value.values()))
            or "index" in next(iter(value.values()))
            or next(iter(value)) in {"scroll_down", "extract_content", "wait", "done"}
        )
    )


class FakeAgent:
    """Replays a recorded run, calling ``on_step_end`` after each step."""

    def __init__(self, items, run_id="run-abc"):
        self.id = run_id
        self._items = items
        self.history = SimpleNamespace(history=[])

    async def run(self, on_step_end=None, max_steps=100):
        for item in self._items[:max_steps]:
            await asyncio.sleep(0)
            self.history.history.append(item)
            if on_step_end is not None:
                await on_step_end(self)
        return self.history


class Recorder:
    def __init__(self, responses=None):
        self.bodies = []
        self.headers = []
        self._responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        if self._responses:
            nxt = self._responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return httpx.Response(
            200, json={"accepted": len(body["steps"]), "duplicates": 0, "rejected": []}
        )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_twelve_recorded_steps_become_twelve_steps_with_screenshots():
    steps = history_to_steps(_history(), run_id="run-abc")

    assert len(steps) == 12
    assert all(s["source"] == "browser_use" for s in steps)
    assert all(s["screenshot"]["content_type"] == "image/png" for s in steps)
    assert [s["source_step_id"] for s in steps][:2] == ["run-abc:1", "run-abc:2"]
    assert [s["action"] for s in steps] == [
        "navigate",
        "type",
        "click",
        "navigate",
        "scroll",
        "click",
        "extract",
        "select",
        "click",
        "type",
        "wait",
        "done",
    ]
    assert steps[1]["target"] == 'input[name="email"]'
    assert steps[0]["url"].endswith("/login")
    assert steps[6]["reasoning"] == "Read the stock level for WH-7781."
    assert steps[0]["occurred_at"].startswith("2026-")


def test_typed_text_is_never_copied_into_the_step():
    text = json.dumps(history_to_steps(_history(), run_id="r"))
    assert "operator@example.test" not in text
    assert '"40"' not in text


def test_live_objects_and_dict_dumps_convert_the_same():
    hist = _history()
    objects = SimpleNamespace(history=[_as_objects(i) for i in hist["history"]])
    assert history_to_steps(objects, run_id="r") == history_to_steps(hist, run_id="r")


def test_failed_result_marks_step_failed_without_error_text():
    hist = _history()
    hist["history"][2]["result"][0]["error"] = "Could not type hunter2 into #pw"
    step = history_to_steps(hist, run_id="r")[2]
    assert step["status"] == "failed"
    assert step["extra"]["error"] == "action_failed"
    assert "hunter2" not in json.dumps(step)


class _Action(pydantic.BaseModel):
    """Stand-in for Browser Use's dynamic ``ActionModel``: one field set."""

    go_to_url: dict | None = None
    input_text: dict | None = None
    click_element_by_index: dict | None = None
    scroll_down: dict | None = None
    extract_content: dict | None = None
    select_dropdown_option: dict | None = None
    wait: dict | None = None
    done: dict | None = None


def test_pydantic_action_models_convert_like_the_dump():
    hist = _history()
    items = []
    for raw in hist["history"]:
        item = _as_objects(raw)
        item.model_output.action = [_Action(**a) for a in raw["model_output"]["action"]]
        items.append(item)
    live = history_to_steps(SimpleNamespace(history=items), run_id="r")
    assert live == history_to_steps(hist, run_id="r")
    assert live[1]["extra"]["browser_use_actions"] == ["input_text"]


def _path_history(tmp_path):
    hist = _history()
    for i, item in enumerate(hist["history"]):
        png = base64.b64decode(item["state"].pop("screenshot"))
        path = tmp_path / f"step_{i}.png"
        path.write_bytes(png)
        item["state"]["screenshot_path"] = str(path)
    return hist


def test_screenshot_path_is_read_in_the_worker_thread(tmp_path, monkeypatch):
    hist = _path_history(tmp_path)
    deferred = history_item_to_step(
        hist["history"][0], run_id="r", index=0, read_files=False
    )
    assert "screenshot" not in deferred and deferred["_screenshot_path"]

    loop_thread = threading.get_ident()
    reads = []
    real = Path.read_bytes

    def tracking(self):
        reads.append(threading.get_ident())
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", tracking)
    rec = Recorder()
    reporter = PreloopBrowserUseReporter(TARGET, batch_size=5, client=_client(rec))
    asyncio.run(reporter.run(FakeAgent(hist["history"])))

    posted = [s for b in rec.bodies for s in b["steps"]]
    assert len(posted) == 12 and all(
        s["screenshot"]["content_type"] == "image/png" for s in posted
    )
    assert all("_screenshot_path" not in s for s in posted)
    assert len(reads) == 12 and loop_thread not in reads


def test_image_rejected_rows_are_resent_without_the_image(caplog):
    rec = Recorder(
        [
            httpx.Response(
                200,
                json={
                    "accepted": 1,
                    "duplicates": 0,
                    "rejected": [{"index": 1, "error": "screenshot_too_large"}],
                },
            ),
        ]
    )
    steps = history_to_steps(_history(), run_id="r")[:2]
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        [result] = StepPoster(TARGET, client=_client(rec)).post(steps)
    assert len(rec.bodies) == 2
    resent = rec.bodies[1]["steps"]
    assert [s["source_step_id"] for s in resent] == ["r:2"]
    assert "screenshot" not in resent[0]
    assert resent[0]["extra"]["screenshot_omitted"] == "screenshot_too_large"
    assert result.accepted == 2 and result.rejected == []
    assert "without them" in caplog.text


def test_other_rejections_are_logged_as_warnings(caplog):
    rec = Recorder(
        [
            httpx.Response(
                200,
                json={
                    "accepted": 0,
                    "duplicates": 0,
                    "rejected": [{"index": 0, "error": "extra_too_large"}],
                },
            )
        ]
    )
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        [result] = StepPoster(TARGET, client=_client(rec)).post(
            [{"source_step_id": "x"}]
        )
    assert result.rejected == [{"index": 0, "error": "extra_too_large"}]
    assert "extra_too_large" in caplog.text and len(rec.bodies) == 1


def test_run_closes_the_client_it_created_but_not_one_passed_in(monkeypatch):
    created = []
    real = httpx.Client

    def factory(*args, **kwargs):
        client = real(transport=httpx.MockTransport(Recorder()))
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    owned = PreloopBrowserUseReporter(TARGET)
    asyncio.run(owned.run(FakeAgent(_history()["history"][:2])))
    assert created and created[0].is_closed

    mine = real(transport=httpx.MockTransport(Recorder()))
    passed = PreloopBrowserUseReporter(TARGET, client=mine)
    asyncio.run(passed.run(FakeAgent(_history()["history"][:2])))
    assert not mine.is_closed


def test_reporter_posts_twelve_steps_in_batches_with_the_agent_key():
    rec = Recorder()
    reporter = PreloopBrowserUseReporter(TARGET, batch_size=5, client=_client(rec))
    agent = FakeAgent(_history()["history"])

    asyncio.run(reporter.run(agent))

    assert [len(b["steps"]) for b in rec.bodies] == [5, 5, 2]
    posted = [s for b in rec.bodies for s in b["steps"]]
    assert [s["step_index"] for s in posted] == list(range(12))
    assert all("screenshot" in s for s in posted)
    assert rec.headers[0]["authorization"] == "Bearer agent-key"
    assert sum(r.accepted for r in reporter.results) == 12


def test_preloop_down_agent_completes_with_one_warning_per_batch(caplog):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    poster = StepPoster(TARGET, client=_client(down), attempts=3, sleep=lambda _: None)
    reporter = PreloopBrowserUseReporter(None, poster=poster, batch_size=5)
    agent = FakeAgent(_history()["history"])

    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        history = asyncio.run(reporter.run(agent))

    assert len(history.history) == 12
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert all("dropped" in r.getMessage() for r in warnings)


def test_retryable_status_is_retried_with_doubling_backoff():
    rec = Recorder([httpx.Response(503), httpx.Response(502)])
    delays = []
    poster = StepPoster(
        TARGET, client=_client(rec), attempts=3, backoff=0.5, sleep=delays.append
    )
    [result] = poster.post([{"source_step_id": "x"}])
    assert result.ok and len(rec.bodies) == 3
    assert delays == [0.5, 1.0]


def test_client_errors_are_not_retried(caplog):
    rec = Recorder([httpx.Response(403)])
    poster = StepPoster(TARGET, client=_client(rec), sleep=lambda _: None)
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        [result] = poster.post([{"source_step_id": "x"}])
    assert not result.ok and len(rec.bodies) == 1
    assert "HTTP 403" in caplog.text


def test_more_than_two_hundred_steps_are_split():
    rec = Recorder()
    StepPoster(TARGET, client=_client(rec)).post([{"i": i} for i in range(450)])
    assert [len(b["steps"]) for b in rec.bodies] == [200, 200, 50]


def test_from_env_without_settings_disables_reporting(monkeypatch, caplog):
    for name in (
        "PRELOOP_URL",
        "PRELOOP_AGENT_KEY",
        "PRELOOP_API_KEY",
        "PRELOOP_RUNTIME_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        reporter = PreloopBrowserUseReporter.from_env()
    history = asyncio.run(reporter.run(FakeAgent(_history()["history"])))
    assert not reporter.enabled and len(history.history) == 12
    assert "PRELOOP_RUNTIME_SESSION_ID" in caplog.text


@pytest.mark.parametrize(
    ("base", "expected"),
    [("http://preloop.test", STEPS_URL), ("http://preloop.test/api/v1/", STEPS_URL)],
)
def test_steps_url_accepts_base_with_or_without_api_prefix(base, expected):
    assert PreloopTarget(base, "k", "sess-1").steps_url == expected


def test_user_hook_still_runs_after_the_reporter():
    seen = []

    async def mine(agent):
        seen.append(len(agent.history.history))

    reporter = PreloopBrowserUseReporter(TARGET, client=_client(Recorder()))
    asyncio.run(reporter.run(FakeAgent(_history()["history"][:3]), on_step_end=mine))
    assert seen == [1, 2, 3]


def test_rows_refused_again_on_retry_keep_their_original_index():
    rec = Recorder(
        [
            httpx.Response(
                200,
                json={
                    "accepted": 4,
                    "duplicates": 0,
                    "rejected": [
                        {"index": 2, "error": "screenshot_invalid"},
                        {"index": 5, "error": "screenshot_too_large"},
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "accepted": 1,
                    "duplicates": 0,
                    "rejected": [{"index": 1, "error": "extra_too_large"}],
                },
            ),
        ]
    )
    steps = [{"source_step_id": str(i), "screenshot": {"x": 1}} for i in range(6)]
    [result] = StepPoster(TARGET, client=_client(rec)).post(steps)
    assert result.accepted == 5
    assert result.rejected == [{"index": 5, "error": "extra_too_large"}]


def test_finish_uploads_timestamped_gif_and_retries(tmp_path: Any) -> None:
    from PIL import Image
    from datetime import datetime

    requests = []
    attempts = 0

    def handle(request: Any) -> Any:
        nonlocal attempts
        requests.append(request)
        if request.url.path.endswith("/browser-steps"):
            return httpx.Response(
                200,
                json={
                    "accepted": len(json.loads(request.content)["steps"]),
                    "duplicates": 0,
                    "rejected": [],
                },
            )
        attempts += 1
        return httpx.Response(500 if attempts == 1 else 201, json={"id": "gif"})

    agent = FakeAgent(_history()["history"][:3])
    poster = StepPoster(TARGET, client=_client(handle), sleep=lambda _: None)
    reporter = PreloopBrowserUseReporter(TARGET, poster=poster)

    async def run():
        await agent.run(on_step_end=reporter.on_step_end)
        await reporter.finish(agent)
        await reporter.finish(agent)

    asyncio.run(run())
    assert reporter.recording_uploaded
    uploads = [r for r in requests if r.url.path.endswith("/artifacts")]
    assert len(uploads) == 2
    metadata = json.loads(
        uploads[-1].content.split(b'name="metadata"\r\n\r\n')[1].split(b"\r\n--")[0]
    )
    assert metadata["kind"] == "screencast"
    timing = metadata["media_timing"]
    assert timing["clock"] == "agent"
    start = _history()["history"][0]["metadata"]["step_start_time"]
    assert datetime.fromisoformat(timing["started_at"]).timestamp() == start
    assert [o["offset_ms"] for o in timing["step_offsets"]] == [
        round((step["metadata"]["step_end_time"] - start) * 1000)
        for step in _history()["history"][:3]
    ]
    body = uploads[-1].content
    gif = body[body.index(b"GIF89a") :].split(b"\r\n--")[0]
    image = Image.open(__import__("io").BytesIO(gif))
    total = 0
    for frame in range(image.n_frames):
        image.seek(frame)
        total += image.info["duration"]
    assert total == timing["duration_ms"]
    assert (
        uploads[0].headers["idempotency-key"] == uploads[1].headers["idempotency-key"]
    )
    # No typed input, task prompt or reasoning overlay is copied to metadata.
    assert "input_text" not in json.dumps(metadata)


def test_finish_external_video_requires_explicit_timing_and_never_raises(
    tmp_path: Any,
) -> None:
    path = tmp_path / "capture.webm"
    path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 20)
    calls = []

    def handle(request: Any) -> Any:
        calls.append(request)
        if request.url.path.endswith("/browser-steps"):
            return httpx.Response(
                200, json={"accepted": 0, "duplicates": 0, "rejected": []}
            )
        return httpx.Response(500)

    reporter = PreloopBrowserUseReporter(
        TARGET, poster=StepPoster(TARGET, client=_client(handle), sleep=lambda _: None)
    )
    agent = FakeAgent([])
    asyncio.run(reporter.finish(agent, recording_path=path))
    assert len(calls) == 3
    assert not reporter.recording_uploaded
    assert b"media_timing" not in calls[0].content
    calls.clear()
    asyncio.run(reporter.finish(agent, gif=False))
    assert calls == []


def test_history_gif_without_timing_is_plain(tmp_path: Any) -> None:
    from preloop_browser_use.recording import history_gif

    steps = [
        history_item_to_step(item, run_id="plain-run", index=i)
        for i, item in enumerate(_history()["history"][:2])
    ]
    path = tmp_path / "plain.gif"
    assert history_gif(steps, None, path) is None
    assert path.read_bytes().startswith(b"GIF")


# --- Runtime session discovery without PRELOOP_RUNTIME_SESSION_ID (#1358) ---

RESOLVED = "00000000-0000-4000-8000-0000000000bb"


def _resolve_transport(calls: list, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"detail": "nope"})
        return httpx.Response(
            200, json={"runtime_session_id": RESOLVED, "created": True}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_from_env_resolves_session_from_client_session_key():
    calls: list = []
    env = {
        "PRELOOP_URL": "http://preloop.test",
        "PRELOOP_API_KEY": "agent-key",
        "PRELOOP_SESSION_ID": "run-42",
    }
    target = PreloopTarget.from_env(env, client=_resolve_transport(calls))
    assert target is not None and target.runtime_session_id == RESOLVED
    (request,) = calls
    assert request.url.path == "/api/v1/runtime-sessions/resolve"
    assert request.url.params["client_session_id"] == "run-42"
    assert request.url.params["create"] == "true"
    assert request.headers["Authorization"] == "Bearer agent-key"


def test_explicit_runtime_session_id_keeps_priority():
    calls: list = []
    env = {
        "PRELOOP_URL": "http://preloop.test",
        "PRELOOP_API_KEY": "agent-key",
        "PRELOOP_SESSION_ID": "run-42",
        "PRELOOP_RUNTIME_SESSION_ID": "sess-1",
    }
    target = PreloopTarget.from_env(env, client=_resolve_transport(calls))
    assert target is not None and target.runtime_session_id == "sess-1"
    assert calls == []


def test_resolve_failure_disables_reporting_without_raising(caplog):
    calls: list = []
    env = {
        "PRELOOP_URL": "http://preloop.test",
        "PRELOOP_API_KEY": "agent-key",
        "PRELOOP_SESSION_ID": "run-42",
    }
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        target = PreloopTarget.from_env(env, client=_resolve_transport(calls, 404))
    assert target is None and len(calls) == 1
    assert "resolve" in caplog.text


def test_gateway_response_hook_captures_session_header():
    from preloop_browser_use import RuntimeSessionCapture

    capture = RuntimeSessionCapture()
    capture(httpx.Response(200, headers={"X-Preloop-Runtime-Session-Id": RESOLVED}))
    capture(httpx.Response(200))
    assert capture.runtime_session_id == RESOLVED
    target = capture.target("http://preloop.test", "agent-key")
    assert target is not None and target.runtime_session_id == RESOLVED
    assert RuntimeSessionCapture().target("http://preloop.test", "k") is None


def test_resolve_timeout_is_short_by_default():
    import inspect

    from preloop_browser_use import resolve_runtime_session_id

    default = inspect.signature(resolve_runtime_session_id).parameters["timeout"]
    assert default.default <= 3.0


@pytest.mark.parametrize(
    "malformed_url", ["https://example.com:invalid-port", "https://exa\nmple.com"]
)
def test_invalid_resolve_url_disables_reporting(
    malformed_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list = []
    env = {
        "PRELOOP_URL": malformed_url,
        "PRELOOP_AGENT_KEY": "agent-key",
        "PRELOOP_SESSION_ID": "run-42",
    }
    # Keep httpx's URL parsing intact: invalid URLs fail before transport.
    with _resolve_transport(calls) as client:
        target = PreloopTarget.from_env(env, client=client)
    assert target is None
    assert calls == []
    assert "Preloop session resolve failed: InvalidURL" in caplog.text


# --- #1568: name the host's desktop endpoint on the session ----------------


def test_desktop_endpoint_comes_from_env_then_desktop_json(tmp_path):
    from preloop_browser_use.client import local_desktop_endpoint_id

    (tmp_path / ".preloop").mkdir()
    state = tmp_path / ".preloop" / "desktop.json"
    assert local_desktop_endpoint_id({}, home=tmp_path) is None
    state.write_text('{"endpoint_id": "dep_file"}')
    assert local_desktop_endpoint_id({}, home=tmp_path) == "dep_file"
    env = {"PRELOOP_DESKTOP_ENDPOINT_ID": " dep_env "}
    assert local_desktop_endpoint_id(env, home=tmp_path) == "dep_env"
    state.write_text("not json")
    assert local_desktop_endpoint_id({}, home=tmp_path) is None


def test_reporter_links_the_session_to_the_desktop_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.method == "PUT":
            return httpx.Response(200, json={"linked": True})
        body = json.loads(request.content)
        return httpx.Response(200, json={"accepted": len(body["steps"])})

    target = PreloopTarget(
        "http://preloop.test", "agent-key", "sess-1", desktop_endpoint_id="dep_abc"
    )
    reporter = PreloopBrowserUseReporter(target, batch_size=5, client=_client(handler))
    asyncio.run(reporter.run(FakeAgent(_history()["history"])))

    puts = [c for c in calls if c[0] == "PUT"]
    assert puts == [
        (
            "PUT",
            "/api/v1/runtime-sessions/sess-1/desktop-endpoint",
            {"desktop_endpoint_id": "dep_abc"},
        )
    ]
    assert calls[0][0] == "PUT"
    assert len([c for c in calls if c[0] == "POST"]) == 3


def test_no_desktop_means_no_link_request_and_a_failed_link_is_harmless(caplog):
    rec = Recorder()
    reporter = PreloopBrowserUseReporter(TARGET, batch_size=5, client=_client(rec))
    asyncio.run(reporter.run(FakeAgent(_history()["history"])))
    assert len(rec.bodies) == 3  # steps only

    def down(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ConnectError("down")
        body = json.loads(request.content)
        return httpx.Response(200, json={"accepted": len(body["steps"])})

    target = PreloopTarget("http://preloop.test", "k", "sess-1", "dep_abc")
    reporter = PreloopBrowserUseReporter(target, batch_size=5, client=_client(down))
    with caplog.at_level(logging.WARNING, logger="preloop_browser_use"):
        asyncio.run(reporter.run(FakeAgent(_history()["history"])))
    assert sum(r.accepted for r in reporter.results) == 12
    assert caplog.text.count("desktop") == 1


def test_capture_target_carries_the_desktop_endpoint(monkeypatch):
    from preloop_browser_use import RuntimeSessionCapture

    monkeypatch.setenv("PRELOOP_DESKTOP_ENDPOINT_ID", "dep_capture")
    capture = RuntimeSessionCapture()
    capture(httpx.Response(200, headers={"X-Preloop-Runtime-Session-Id": RESOLVED}))
    target = capture.target("http://preloop.test", "agent-key")
    assert target.desktop_endpoint_id == "dep_capture"
