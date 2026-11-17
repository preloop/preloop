"""Tests for audio model provider backends."""

import json
from io import BytesIO
from types import SimpleNamespace
from urllib import error as urllib_error

import pytest

from preloop.services.audio_model import (
    AudioModelResolution,
    GoogleSpeechAudioProviderBackend,
)


def test_google_speech_backend_posts_recognize_payload(monkeypatch):
    """Google STT should call Speech-to-Text with the configured model."""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "results": [
                        {"alternatives": [{"transcript": "hello hermes"}]},
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "preloop.services.audio_model.urllib_request.urlopen", fake_urlopen
    )

    resolution = AudioModelResolution(
        ai_model=SimpleNamespace(id="model-id"),
        model_identifier="latest_short",
        provider_name="google",
        api_key="google-key",
        api_endpoint=None,
        model_parameters={"language_code": "en-US"},
    )

    transcript = GoogleSpeechAudioProviderBackend().transcribe(
        resolution=resolution,
        audio=b"audio-bytes",
        filename="clip.webm",
        content_type="audio/webm",
    )

    assert transcript == "hello hermes"
    assert captured["url"] == ("https://speech.googleapis.com/v1/speech:recognize")
    assert captured["headers"]["X-goog-api-key"] == "google-key"
    assert "key=" not in captured["url"]
    assert captured["timeout"] == 60
    assert captured["body"]["config"]["model"] == "latest_short"
    assert captured["body"]["config"]["encoding"] == "WEBM_OPUS"
    assert captured["body"]["audio"]["content"]


def test_google_speech_backend_hides_http_error_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Google STT failures should not leak provider response bodies to callers."""

    class FakeHTTPError(urllib_error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                url="https://speech.googleapis.com/v1/speech:recognize",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=BytesIO(b'{"error":{"message":"secret project id"}}'),
            )

        def read(self) -> bytes:
            return b'{"error":{"message":"secret project id"}}'

    monkeypatch.setattr(
        "preloop.services.audio_model.urllib_request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FakeHTTPError()),
    )

    resolution = AudioModelResolution(
        ai_model=SimpleNamespace(id="model-id"),
        model_identifier="latest_short",
        provider_name="google",
        api_key="google-key",
        api_endpoint=None,
        model_parameters={"language_code": "en-US"},
    )

    with pytest.raises(ValueError, match="^Google STT request failed$"):
        GoogleSpeechAudioProviderBackend().transcribe(
            resolution=resolution,
            audio=b"audio-bytes",
            filename="clip.webm",
            content_type="audio/webm",
        )
    assert "secret project id" not in caplog.text
    assert all(
        "secret project id" not in repr(record.args) for record in caplog.records
    )
