"""Narrow, stateless authority to validate documents against one saved schema."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt

from preloop.config import settings

AUDIENCE = "native-result-validation"
VALIDATION_PATH = "/api/v1/internal/result-validation"


def schema_digest(schema: Any) -> str:
    """Bind the capability to the exact schema selected at launch."""
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def result_validation_environment(
    schema: dict[str, Any], *, api_url: str | None = None
) -> dict[str, str]:
    """Give the wrapper validation authority without account or model access."""
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=24, minutes=5),
            "schema_digest": schema_digest(schema),
            "jti": str(uuid4()),
        },
        settings.security.secret_key,
        algorithm="HS256",
    )
    url = os.getenv("PRELOOP_EXECUTION_API_URL") or api_url or settings.preloop_url
    return {
        "PRELOOP_RESULT_VALIDATION_URL": url.rstrip("/") + VALIDATION_PATH,
        "PRELOOP_RESULT_VALIDATION_CAPABILITY": token,
    }
