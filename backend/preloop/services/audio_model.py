"""Resolve and invoke speech-capable AI models."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import UUID

import openai
from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model
from preloop.models.models.ai_model import AIModel
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

MODEL_KIND_LLM = "llm"
MODEL_KIND_STT = "stt"
MODEL_KIND_TTS = "tts"
SUPPORTED_MODEL_KINDS = {MODEL_KIND_LLM, MODEL_KIND_STT, MODEL_KIND_TTS}
OPENAI_AUDIO_PROVIDERS = {"openai", "custom", "openai-compatible"}
GOOGLE_AUDIO_PROVIDERS = {"google"}
GOOGLE_SPEECH_RECOGNIZE_URL = "https://speech.googleapis.com/v1/speech:recognize"

logger = logging.getLogger(__name__)


@dataclass
class AudioModelResolution:
    """Resolved speech model plus direct-provider runtime configuration."""

    ai_model: AIModel
    model_identifier: str
    provider_name: str
    api_key: str
    api_endpoint: Optional[str]
    model_parameters: dict[str, Any]


class AudioProviderBackend(Protocol):
    """Backend interface for provider-specific audio APIs."""

    def transcribe(
        self,
        *,
        resolution: AudioModelResolution,
        audio: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        pass

    def synthesize(
        self,
        *,
        resolution: AudioModelResolution,
        text: str,
        voice: str,
        response_format: str,
    ) -> bytes:
        pass


class OpenAIAudioProviderBackend:
    """OpenAI-compatible STT/TTS backend."""

    def _client(self, resolution: AudioModelResolution) -> openai.OpenAI:
        return openai.OpenAI(
            api_key=resolution.api_key,
            base_url=resolution.api_endpoint or None,
        )

    def transcribe(
        self,
        *,
        resolution: AudioModelResolution,
        audio: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        params = dict(resolution.model_parameters or {})
        result = self._client(resolution).audio.transcriptions.create(
            model=resolution.model_identifier,
            file=(filename, audio, content_type),
            **params,
        )
        text = getattr(result, "text", None)
        if isinstance(text, str):
            return text
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            return result["text"]
        raise ValueError("STT provider response did not include transcript text")

    def synthesize(
        self,
        *,
        resolution: AudioModelResolution,
        text: str,
        voice: str,
        response_format: str,
    ) -> bytes:
        params = dict(resolution.model_parameters or {})
        params.setdefault("voice", voice)
        params.setdefault("response_format", response_format)
        result = self._client(resolution).audio.speech.create(
            model=resolution.model_identifier,
            input=text,
            **params,
        )
        content = getattr(result, "content", None)
        if isinstance(content, bytes):
            return content
        read = getattr(result, "read", None)
        if callable(read):
            data = read()
            if isinstance(data, bytes):
                return data
        raise ValueError("TTS provider response did not include audio bytes")


class GoogleSpeechAudioProviderBackend:
    """Google Cloud Speech-to-Text backend using API-key credentials."""

    def transcribe(
        self,
        *,
        resolution: AudioModelResolution,
        audio: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        params = dict(resolution.model_parameters or {})
        language_code = str(params.pop("language_code", "en-US"))
        recognition_config: dict[str, Any] = {
            "languageCode": language_code,
            "enableAutomaticPunctuation": params.pop(
                "enable_automatic_punctuation", True
            ),
            **params,
        }
        encoding = _google_audio_encoding(content_type, filename)
        if encoding:
            recognition_config["encoding"] = encoding
        if resolution.model_identifier:
            recognition_config["model"] = resolution.model_identifier

        payload = {
            "config": recognition_config,
            "audio": {"content": base64.b64encode(audio).decode("ascii")},
        }
        request = urllib_request.Request(
            GOOGLE_SPEECH_RECOGNIZE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": resolution.api_key,
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=60) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.warning("Google STT request failed: status=%s", exc.code)
            raise ValueError("Google STT request failed") from exc
        except urllib_error.URLError as exc:
            logger.warning("Google STT request failed: %s", type(exc).__name__)
            raise ValueError("Google STT request failed") from exc

        transcripts = [
            alternative.get("transcript", "")
            for result in response_payload.get("results", [])
            for alternative in result.get("alternatives", [])[:1]
            if isinstance(alternative.get("transcript"), str)
        ]
        transcript = " ".join(part.strip() for part in transcripts if part.strip())
        if transcript:
            return transcript
        raise ValueError("Google STT provider response did not include transcript text")

    def synthesize(
        self,
        *,
        resolution: AudioModelResolution,
        text: str,
        voice: str,
        response_format: str,
    ) -> bytes:
        raise ValueError("Google TTS is not supported yet")


def _google_audio_encoding(content_type: str, filename: str) -> Optional[str]:
    normalized_content_type = content_type.split(";")[0].strip().lower()
    normalized_filename = filename.lower()
    if normalized_content_type in {
        "audio/webm",
        "video/webm",
    } or normalized_filename.endswith(".webm"):
        return "WEBM_OPUS"
    if normalized_content_type in {
        "audio/ogg",
        "application/ogg",
    } or normalized_filename.endswith(".ogg"):
        return "OGG_OPUS"
    if normalized_content_type in {
        "audio/wav",
        "audio/x-wav",
    } or normalized_filename.endswith(".wav"):
        return "LINEAR16"
    if normalized_content_type == "audio/flac" or normalized_filename.endswith(".flac"):
        return "FLAC"
    if normalized_content_type in {
        "audio/mpeg",
        "audio/mp3",
    } or normalized_filename.endswith(".mp3"):
        return "MP3"
    return None


class AudioModelService:
    """Resolve account speech models and call the selected provider."""

    def __init__(
        self,
        db: Session,
        *,
        backend: Optional[AudioProviderBackend] = None,
    ) -> None:
        self.db = db
        self.backend = backend
        self.openai_backend = OpenAIAudioProviderBackend()
        self.google_backend = GoogleSpeechAudioProviderBackend()

    @staticmethod
    def normalize_model_kind(model_kind: str) -> str:
        normalized = model_kind.strip().lower()
        if normalized not in {MODEL_KIND_STT, MODEL_KIND_TTS}:
            raise ValueError("Audio model kind must be stt or tts")
        return normalized

    def resolve_model(
        self,
        *,
        account_id: UUID,
        model_kind: str,
        model_id: Optional[UUID] = None,
    ) -> AudioModelResolution:
        """Resolve explicit, account-default, then installation-default audio model."""
        normalized_model_kind = self.normalize_model_kind(model_kind)
        ai_model = self._select_ai_model(
            account_id=account_id,
            model_kind=normalized_model_kind,
            model_id=model_id,
        )
        runtime = resolve_ai_model_runtime(ai_model, allow_gateway=False)
        provider_name = (runtime.model_provider or ai_model.provider_name or "").lower()
        if provider_name not in OPENAI_AUDIO_PROVIDERS | GOOGLE_AUDIO_PROVIDERS:
            raise ValueError(
                f"Audio provider '{provider_name}' is not supported for server fallback"
            )
        if not runtime.model_identifier:
            raise ValueError("Resolved audio model is missing model_identifier")
        if not runtime.model_api_key:
            raise ValueError("Resolved audio model is missing provider credentials")

        return AudioModelResolution(
            ai_model=ai_model,
            model_identifier=runtime.model_identifier,
            provider_name=provider_name,
            api_key=runtime.model_api_key,
            api_endpoint=runtime.model_endpoint,
            model_parameters=runtime.model_parameters or {},
        )

    def transcribe(
        self,
        *,
        account_id: UUID,
        audio: bytes,
        filename: str,
        content_type: str,
        model_id: Optional[UUID] = None,
    ) -> tuple[str, AudioModelResolution]:
        resolution = self.resolve_model(
            account_id=account_id, model_kind=MODEL_KIND_STT, model_id=model_id
        )
        transcript = self._backend_for(
            resolution=resolution, model_kind=MODEL_KIND_STT
        ).transcribe(
            resolution=resolution,
            audio=audio,
            filename=filename,
            content_type=content_type,
        )
        return transcript, resolution

    def synthesize(
        self,
        *,
        account_id: UUID,
        text: str,
        voice: str,
        response_format: str,
        model_id: Optional[UUID] = None,
    ) -> tuple[bytes, AudioModelResolution]:
        resolution = self.resolve_model(
            account_id=account_id, model_kind=MODEL_KIND_TTS, model_id=model_id
        )
        audio = self._backend_for(
            resolution=resolution, model_kind=MODEL_KIND_TTS
        ).synthesize(
            resolution=resolution,
            text=text,
            voice=voice,
            response_format=response_format,
        )
        return audio, resolution

    def _backend_for(
        self, *, resolution: AudioModelResolution, model_kind: str
    ) -> AudioProviderBackend:
        if self.backend is not None:
            return self.backend
        if resolution.provider_name in OPENAI_AUDIO_PROVIDERS:
            return self.openai_backend
        if (
            resolution.provider_name in GOOGLE_AUDIO_PROVIDERS
            and model_kind == MODEL_KIND_STT
        ):
            return self.google_backend
        raise ValueError(
            f"Audio provider '{resolution.provider_name}' is not supported for {model_kind.upper()}"
        )

    def _select_ai_model(
        self,
        *,
        account_id: UUID,
        model_kind: str,
        model_id: Optional[UUID],
    ) -> AIModel:
        if model_id is not None:
            ai_model = crud_ai_model.get(db=self.db, id=model_id)
            if (
                ai_model is None
                or ai_model.account_id not in {account_id, None}
                or ai_model.model_kind != model_kind
            ):
                raise LookupError(f"No {model_kind.upper()} model is available")
            return ai_model

        candidates = crud_ai_model.get_all_for_account(
            self.db,
            account_id=account_id,
        )
        account_default = next(
            (
                model
                for model in candidates
                if model.account_id == account_id
                and model.is_default
                and model.model_kind == model_kind
            ),
            None,
        )
        if account_default is not None:
            return account_default

        installation_default = next(
            (
                model
                for model in candidates
                if model.account_id is None
                and model.is_default
                and model.model_kind == model_kind
            ),
            None,
        )
        if installation_default is not None:
            return installation_default

        account_models = [
            model
            for model in candidates
            if model.account_id == account_id and model.model_kind == model_kind
        ]
        if len(account_models) == 1:
            return account_models[0]

        raise LookupError(f"No {model_kind.upper()} model is available")
