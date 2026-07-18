"""Tests for server-side audio fallback endpoints."""

from preloop.api.endpoints import audio as audio_endpoint
from preloop.models.crud import crud_ai_model
from preloop.services import audio_model as audio_model_module


class FakeAudioBackend:
    """Deterministic audio backend for endpoint tests."""

    def transcribe(self, *, resolution, audio, filename, content_type):
        assert audio == b"audio-bytes"
        assert filename == "clip.webm"
        assert content_type == "audio/webm"
        return f"transcribed by {resolution.model_identifier}"

    def synthesize(self, *, resolution, text, voice, response_format):
        assert voice == "verse"
        assert response_format == "mp3"
        return f"{resolution.model_identifier}:{text}".encode()


def test_transcribe_audio_uses_account_default_stt_model(
    client,
    db_session,
    test_user,
    monkeypatch,
):
    """STT uploads should resolve the account default STT model."""
    monkeypatch.setattr(
        audio_model_module,
        "OpenAIAudioProviderBackend",
        FakeAudioBackend,
    )
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Chat Default",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4-mini",
            "api_key": "llm-key",
            "is_default": True,
            "model_kind": "llm",
        },
        account_id=test_user.account_id,
    )
    stt_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Whisper Default",
            "provider_name": "openai",
            "model_identifier": "whisper-1",
            "api_key": "stt-key",
            "is_default": True,
            "model_kind": "stt",
        },
        account_id=test_user.account_id,
    )

    response = client.post(
        "/api/v1/audio/transcriptions",
        files={"audio": ("clip.webm", b"audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "transcribed by whisper-1"
    assert body["ai_model_id"] == str(stt_model.id)
    assert body["provider_name"] == "openai"


def test_transcribe_audio_uses_only_account_stt_model_when_no_default(
    client,
    db_session,
    test_user,
    monkeypatch,
):
    """A single configured STT model should be usable even before it is made default."""
    monkeypatch.setattr(
        audio_model_module,
        "OpenAIAudioProviderBackend",
        FakeAudioBackend,
    )
    stt_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Whisper",
            "provider_name": "openai",
            "model_identifier": "whisper-1",
            "api_key": "stt-key",
            "model_kind": "stt",
        },
        account_id=test_user.account_id,
    )

    response = client.post(
        "/api/v1/audio/transcriptions",
        files={"audio": ("clip.webm", b"audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "transcribed by whisper-1"
    assert body["ai_model_id"] == str(stt_model.id)


def test_synthesize_speech_falls_back_to_installation_default_tts_model(
    client,
    db_session,
    test_user,
    monkeypatch,
):
    """TTS should use installation default when no account TTS default exists."""
    monkeypatch.setattr(
        audio_model_module,
        "OpenAIAudioProviderBackend",
        FakeAudioBackend,
    )
    tts_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Installation TTS",
            "provider_name": "openai",
            "model_identifier": "gpt-4o-mini-tts",
            "api_key": "tts-key",
            "is_default": True,
            "model_kind": "tts",
        },
        account_id=None,
    )

    response = client.post(
        "/api/v1/audio/speech",
        json={
            "input": "hello agent",
            "voice": "verse",
            "response_format": "mp3",
        },
    )

    assert response.status_code == 200
    assert response.content == b"gpt-4o-mini-tts:hello agent"
    assert response.headers["x-preloop-ai-model-id"] == str(tts_model.id)
    assert response.headers["content-type"].startswith("audio/mpeg")


def test_transcribe_audio_returns_404_without_stt_model(client):
    """Missing STT configuration should tell the browser to stay native-only."""
    response = client.post(
        "/api/v1/audio/transcriptions",
        files={"audio": ("clip.webm", b"audio-bytes", "audio/webm")},
    )

    assert response.status_code == 404
    assert "No STT model is available" in response.json()["detail"]


def test_transcribe_audio_rejects_oversized_upload(client, monkeypatch):
    """STT uploads larger than the configured limit should be rejected."""
    monkeypatch.setattr(audio_endpoint, "MAX_AUDIO_UPLOAD_BYTES", 8)

    response = client.post(
        "/api/v1/audio/transcriptions",
        files={"audio": ("clip.webm", b"0123456789", "audio/webm")},
    )

    assert response.status_code == 413
    assert "byte limit" in response.json()["detail"]
