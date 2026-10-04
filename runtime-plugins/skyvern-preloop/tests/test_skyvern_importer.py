"""Skyvern importer against recorded Skyvern API v1 responses."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import zipfile

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from skyvern_fakes import SKYVERN, FakePreloop, RecordedSkyvern, recorded  # noqa: E402

from preloop_skyvern import (  # noqa: E402
    PreloopClient,
    PreloopTarget,
    SkyvernClient,
    SkyvernError,
    handle_webhook,
    import_task,
    step_to_browser_step,
)
from preloop_skyvern import cli  # noqa: E402

TASK = "tsk_400712345678901234"
TARGET = PreloopTarget("http://preloop.test", "agent-key", "sess-1")


@pytest.fixture
def skyvern_handler():
    return RecordedSkyvern()


@pytest.fixture
def preloop_handler():
    return FakePreloop()


def _clients(skyvern_handler, preloop_handler):
    skyvern = SkyvernClient(
        "sk-skyvern-test",
        base_url=SKYVERN,
        client=httpx.Client(transport=httpx.MockTransport(skyvern_handler)),
    )
    preloop = PreloopClient(
        TARGET,
        client=httpx.Client(transport=httpx.MockTransport(preloop_handler)),
        sleep=lambda _: None,
    )
    return skyvern, preloop


def _kw(skyvern_handler, preloop_handler):
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    return {"skyvern": skyvern, "preloop": preloop}


def test_recorded_task_produces_the_expected_steps(skyvern_handler, preloop_handler):
    report = import_task(TASK, **_kw(skyvern_handler, preloop_handler))

    steps = list(preloop_handler.steps.values())
    assert report.steps.accepted == 6 and report.screenshots == 6
    assert [s["source_step_id"] for s in steps] == [
        f"{TASK}:stp_40071{i:04d}" for i in range(6)
    ]
    assert [s["action"] for s in steps] == [
        "click",
        "type",
        "select",
        "click",
        "click",
        "done",
    ]
    assert [s["status"] for s in steps] == [
        "success",
        "success",
        "success",
        "failed",
        "success",
        "success",
    ]
    assert all(s["source"] == "skyvern" for s in steps)
    assert steps[0]["url"] == "https://warehouse-sim.example.test/receiving"
    assert steps[3]["extra"]["error"] == "ElementNotFound"
    assert "was detached" not in json.dumps(steps)
    assert steps[4]["extra"]["skyvern_retry_index"] == 1
    assert steps[1]["extra"]["skyvern_actions"] == ["input_text", "input_text"]
    assert steps[0]["occurred_at"] == "2026-09-30T09:00:08.500000+00:00"
    assert all(s["screenshot"]["content_type"] == "image/png" for s in steps)
    assert preloop_handler.auth == {"Bearer agent-key"}


def test_action_screenshot_is_preferred_over_the_model_screenshot(
    skyvern_handler, preloop_handler
):
    import base64

    data = recorded()
    import_task(TASK, **_kw(skyvern_handler, preloop_handler))
    first = next(iter(preloop_handler.steps.values()))
    action_png = data["downloads"]["https://files.skyvern.example.test/s0a.png"]
    assert first["screenshot"]["data_base64"] == action_png
    assert base64.b64decode(action_png).startswith(b"\x89PNG")


def test_typed_text_is_never_copied(skyvern_handler, preloop_handler):
    import_task(TASK, **_kw(skyvern_handler, preloop_handler))
    text = json.dumps(list(preloop_handler.steps.values()))
    assert "typed-receipt-GR-2291-x7" not in text
    assert '"12"' not in text


def test_har_trace_and_recording_are_deposited_with_kinds(
    skyvern_handler, preloop_handler
):
    report = import_task(TASK, **_kw(skyvern_handler, preloop_handler))

    kinds = sorted((a["kind"], a["name"]) for a in report.artifacts)
    assert kinds == [
        ("recording", f"skyvern-{TASK}.webm"),
        ("trace", f"skyvern-{TASK}-har.zip"),
        ("trace", f"skyvern-{TASK}-trace.zip"),
    ]
    assert set(preloop_handler.artifacts) == {
        f"skyvern:{TASK}:a_har",
        f"skyvern:{TASK}:a_trace",
        f"skyvern:{TASK}:a_rec",
    }
    har_body = preloop_handler.artifacts[f"skyvern:{TASK}:a_har"]["body"]
    zipped = har_body.split(b"application/zip\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        assert archive.namelist() == [f"skyvern-{TASK}.har"]
        assert (
            json.loads(archive.read(archive.namelist()[0]))["log"]["version"] == "1.2"
        )
    assert not any("llm_prompt" in s for s in report.skipped)


def test_reimport_creates_no_duplicates(skyvern_handler, preloop_handler):
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    import_task(TASK, skyvern=skyvern, preloop=preloop)

    again = import_task(TASK, skyvern=skyvern, preloop=preloop)

    assert again.steps.accepted == 0 and again.steps.duplicates == 6
    assert len(preloop_handler.steps) == 6 and len(preloop_handler.artifacts) == 3
    assert {a["id"] for a in again.artifacts} == {"art-1", "art-2", "art-3"}


def test_server_without_deposit_api_skips_files_naming_1080(skyvern_handler, caplog):
    preloop_handler = FakePreloop(deposit_route=False)
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    with caplog.at_level(logging.INFO, logger="preloop_skyvern"):
        report = import_task(TASK, skyvern=skyvern, preloop=preloop)
    assert report.steps.accepted == 6 and report.artifacts == []
    assert "#1080" in caplog.text
    assert report.skipped == ["files: artifact deposit API unavailable (#1080)"]


def test_oversized_or_missing_screenshot_keeps_the_step(preloop_handler):
    data = recorded()
    for step_id, arts in data["artifacts"].items():
        data["artifacts"][step_id] = [
            a for a in arts if not a["artifact_type"].startswith("screenshot")
        ]
    skyvern, preloop = _clients(RecordedSkyvern(data), preloop_handler)
    report = import_task(TASK, skyvern=skyvern, preloop=preloop, include_files=False)
    assert report.steps.accepted == 6 and report.screenshots == 0
    assert all("screenshot" not in s for s in preloop_handler.steps.values())


def test_unknown_action_maps_to_other():
    step = {
        "step_id": "s",
        "status": "completed",
        "output": {"actions_and_results": [[{"action_type": "solve_captcha"}, []]]},
    }
    assert (
        step_to_browser_step(step, task={"task_id": "t"}, index=3)["action"] == "other"
    )


def _signed(body: bytes, key: str = "sk-skyvern-test") -> dict:
    return {
        "X-Skyvern-Signature": hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    }


def test_webhook_imports_a_finished_task(skyvern_handler, preloop_handler):
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    body = json.dumps(recorded()["task"]).encode()
    status, result = handle_webhook(
        body,
        _signed(body),
        skyvern=skyvern,
        skyvern_api_key="sk-skyvern-test",
        preloop_for_task=lambda task: preloop,
    )
    assert status == 200 and result["steps_accepted"] == 6
    assert len(result["artifacts"]) == 3


def test_webhook_rejects_a_bad_signature(skyvern_handler, preloop_handler):
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    body = json.dumps(recorded()["task"]).encode()
    status, _ = handle_webhook(
        body,
        _signed(body, "wrong"),
        skyvern=skyvern,
        skyvern_api_key="sk-skyvern-test",
        preloop_for_task=lambda task: preloop,
    )
    assert status == 401 and preloop_handler.steps == {}


def test_webhook_ignores_running_tasks_and_unmapped_sessions(
    skyvern_handler, preloop_handler
):
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    running = json.dumps({"task_id": TASK, "status": "running"}).encode()
    done = json.dumps({"task_id": TASK, "status": "completed"}).encode()
    kwargs = dict(skyvern=skyvern, skyvern_api_key="sk-skyvern-test")
    assert (
        handle_webhook(
            running, _signed(running), preloop_for_task=lambda t: preloop, **kwargs
        )[0]
        == 202
    )
    assert handle_webhook(
        done, _signed(done), preloop_for_task=lambda t: None, **kwargs
    )[1] == {
        "ignored": "no_session",
        "task_id": TASK,
    }
    assert preloop_handler.steps == {}


def test_cli_imports_and_prints_a_summary(
    monkeypatch, capsys, skyvern_handler, preloop_handler
):
    real = httpx.Client

    def routed(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)

        def handler(request):
            if request.url.host == "preloop.test":
                return preloop_handler(request)
            return skyvern_handler(request)

        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", routed)
    monkeypatch.setenv("SKYVERN_API_KEY", "sk-skyvern-test")
    monkeypatch.setenv("PRELOOP_AGENT_KEY", "agent-key")
    code = cli.main(
        [
            "--task",
            TASK,
            "--session",
            "sess-1",
            "--skyvern-url",
            SKYVERN,
            "--preloop-url",
            "http://preloop.test",
        ]
    )
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert "files.skyvern.example.test" not in captured.err
    assert code == 0 and out["steps_accepted"] == 6 and out["screenshots"] == 6


def test_cli_reports_missing_configuration(monkeypatch, capsys):
    for name in (
        "SKYVERN_API_KEY",
        "PRELOOP_AGENT_KEY",
        "PRELOOP_API_KEY",
        "PRELOOP_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["--task", TASK, "--session", "s"]) == 2
    assert "SKYVERN_API_KEY" in capsys.readouterr().err


def test_preloop_outage_is_reported_not_raised(skyvern_handler, caplog):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    skyvern, _ = _clients(skyvern_handler, FakePreloop())
    preloop = PreloopClient(
        TARGET,
        client=httpx.Client(transport=httpx.MockTransport(down)),
        sleep=lambda _: None,
    )
    with caplog.at_level(logging.WARNING, logger="preloop_skyvern"):
        report = import_task(TASK, skyvern=skyvern, preloop=preloop)
    assert report.steps.failed_batches == 1 and report.artifacts == []


def _down(request):
    raise httpx.ConnectError("refused", request=request)


def _skyvern(handler):
    return SkyvernClient(
        "sk-skyvern-test",
        base_url=SKYVERN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_unreachable_skyvern_makes_the_webhook_answer_502(preloop_handler):
    _, preloop = _clients(RecordedSkyvern(), preloop_handler)
    body = json.dumps(recorded()["task"]).encode()
    status, result = handle_webhook(
        body,
        _signed(body),
        skyvern=_skyvern(_down),
        skyvern_api_key="sk-skyvern-test",
        preloop_for_task=lambda task: preloop,
    )
    assert status == 502 and result["error"] == "skyvern_unavailable"


def test_unreachable_skyvern_makes_the_cli_print_a_message(monkeypatch, capsys):
    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: real(transport=httpx.MockTransport(_down))
    )
    monkeypatch.setenv("SKYVERN_API_KEY", "sk-skyvern-test")
    monkeypatch.setenv("PRELOOP_AGENT_KEY", "agent-key")
    code = cli.main(
        [
            "--task",
            TASK,
            "--session",
            "s",
            "--skyvern-url",
            SKYVERN,
            "--preloop-url",
            "http://preloop.test",
        ]
    )
    assert code == 1
    assert "skyvern: GET /tasks/" in capsys.readouterr().err


def test_api_redirects_are_not_followed_with_the_key():
    seen = []

    def redirecting(request):
        seen.append(str(request.url))
        if request.url.host == "skyvern.example.test":
            return httpx.Response(302, headers={"location": "https://elsewhere.test/x"})
        return httpx.Response(200, json={})

    client = SkyvernClient(
        "sk-skyvern-test",
        base_url=SKYVERN,
        client=httpx.Client(
            transport=httpx.MockTransport(redirecting), follow_redirects=True
        ),
    )
    with pytest.raises(SkyvernError, match="HTTP 302"):
        client.get_task(TASK)
    assert seen == [f"{SKYVERN}/api/v1/tasks/{TASK}"]


def test_a_changed_response_shape_fails_with_a_named_error(preloop_handler):
    data = recorded()
    data["steps"] = [{"id": "renamed"}]
    skyvern, preloop = _clients(RecordedSkyvern(data), preloop_handler)
    with pytest.raises(SkyvernError, match="may have changed"):
        import_task(TASK, skyvern=skyvern, preloop=preloop)
    assert preloop_handler.steps == {}


def test_image_rejected_steps_are_resent_without_the_image(skyvern_handler, caplog):
    class RefusesFirstImage(FakePreloop):
        refused = False

        def __call__(self, request):
            if request.url.path.endswith("/browser-steps") and not self.refused:
                self.refused = True
                steps = json.loads(request.content)["steps"]
                for step in steps[1:]:
                    self.steps[f"{step['source']}:{step['source_step_id']}"] = step
                return httpx.Response(
                    200,
                    json={
                        "accepted": len(steps) - 1,
                        "duplicates": 0,
                        "rejected": [{"index": 0, "error": "screenshot_too_large"}],
                    },
                )
            return super().__call__(request)

    preloop_handler = RefusesFirstImage()
    skyvern, preloop = _clients(skyvern_handler, preloop_handler)
    with caplog.at_level(logging.WARNING, logger="preloop_skyvern"):
        report = import_task(
            TASK, skyvern=skyvern, preloop=preloop, include_files=False
        )
    first = preloop_handler.steps[f"skyvern:{TASK}:stp_400710000"]
    assert report.steps.accepted == 6 and report.steps.rejected == []
    assert "screenshot" not in first
    assert first["extra"]["screenshot_omitted"] == "screenshot_too_large"
    assert "without them" in caplog.text
