"""Server-side audio fallback endpoints."""

from __future__ import annotations

import asyncio
import logging
import uuid
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.audio import SpeechToTextResponse, TextToSpeechRequest
from preloop.services.audio_model import AudioModelService
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

TTS_RESPONSE_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/L16",
}
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024


async def _read_upload_with_limit(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks and reject payloads larger than max_bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"audio upload exceeds {max_bytes} byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _audio_model_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    logger.warning("Audio provider call failed")
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Audio provider request failed",
    )


@router.post(
    "/audio/transcriptions",
    response_model=SpeechToTextResponse,
    summary="Transcribe Audio",
    tags=["Audio"],
)
@require_permission("view_ai_models")
async def transcribe_audio(
    audio: UploadFile = File(...),
    ai_model_id: Optional[uuid.UUID] = Form(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> SpeechToTextResponse:
    """Transcribe an uploaded audio blob using the resolved account STT model."""
    audio_bytes = await _read_upload_with_limit(audio, MAX_AUDIO_UPLOAD_BYTES)
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="audio upload is empty",
        )

    try:
        transcript, resolution = await asyncio.to_thread(
            AudioModelService(db).transcribe,
            account_id=current_user.account_id,
            audio=audio_bytes,
            filename=audio.filename or "audio.webm",
            content_type=audio.content_type or "application/octet-stream",
            model_id=ai_model_id,
        )
    except Exception as exc:
        raise _audio_model_error(exc) from exc

    return SpeechToTextResponse(
        text=transcript,
        ai_model_id=resolution.ai_model.id,
        provider_name=resolution.provider_name,
        model_identifier=resolution.model_identifier,
    )


@router.post(
    "/audio/speech",
    summary="Synthesize Speech",
    tags=["Audio"],
)
@require_permission("view_ai_models")
def synthesize_speech(
    request: TextToSpeechRequest,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """Synthesize speech bytes using the resolved account TTS model."""
    try:
        audio_bytes, resolution = AudioModelService(db).synthesize(
            account_id=current_user.account_id,
            text=request.input,
            voice=request.voice,
            response_format=request.response_format,
            model_id=request.ai_model_id,
        )
    except Exception as exc:
        raise _audio_model_error(exc) from exc

    headers = {
        "X-Preloop-AI-Model-Id": str(resolution.ai_model.id),
        "X-Preloop-AI-Provider": resolution.provider_name,
        "X-Preloop-AI-Model": resolution.model_identifier,
    }
    return StreamingResponse(
        BytesIO(audio_bytes),
        media_type=TTS_RESPONSE_MEDIA_TYPES[request.response_format],
        headers=headers,
    )
