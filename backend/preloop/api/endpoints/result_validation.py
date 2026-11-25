"""Runner-only schema validation using the same validator as final capture."""

import json
from typing import Any

import jwt
from fastapi import APIRouter, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from preloop.config import settings
from preloop.services.flow_artifacts import sanitize_captured_result
from preloop.services.flow_result_schema import validate_result_document
from preloop.services.result_validation_capability import AUDIENCE, schema_digest

router = APIRouter()
MAX_REQUEST_BYTES = 384 * 1024


@router.post("/internal/result-validation", include_in_schema=False)
async def validate_native_result(
    request: Request,
    response: Response,
    x_preloop_result_capability: str = Header(default=""),
) -> dict[str, Any]:
    """Validate caller-supplied data; never read or persist execution data.

    The capability permits only the exact schema selected at launch. It
    conveys no account access, posting authority, or model credentials.
    """
    try:
        claims = jwt.decode(
            x_preloop_result_capability,
            settings.security.secret_key,
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={"require": ["exp", "schema_digest", "aud"]},
        )
        if claims["aud"] != AUDIENCE or not isinstance(claims["schema_digest"], str):
            raise ValueError("invalid capability")
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "invalid_result_validation_capability") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "result_validation_request_too_large")
    try:
        payload = json.loads(body)
        schema, document = payload["schema"], payload["document"]
        if not isinstance(schema, dict) or not isinstance(document, dict):
            raise ValueError("objects required")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "invalid_result_validation_request") from exc
    if schema_digest(schema) != claims["schema_digest"]:
        raise HTTPException(403, "result_validation_schema_mismatch")
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        validate_result_document, sanitize_captured_result(document), schema
    )
