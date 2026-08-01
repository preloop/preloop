"""Provider-native error envelopes for model gateway endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional


GatewayProvider = Literal["openai", "anthropic", "gemini"]


def _default_error_type(provider: GatewayProvider, status_code: int) -> str:
    if provider == "gemini":
        if status_code == 400:
            return "INVALID_ARGUMENT"
        if status_code == 401:
            return "UNAUTHENTICATED"
        if status_code == 403:
            return "PERMISSION_DENIED"
        if status_code == 404:
            return "NOT_FOUND"
        if status_code == 429:
            return "RESOURCE_EXHAUSTED"
        if status_code >= 500:
            return "INTERNAL"
        return "UNKNOWN"

    if provider == "anthropic":
        if status_code == 400:
            return "invalid_request_error"
        if status_code == 401:
            return "authentication_error"
        if status_code == 403:
            return "permission_error"
        if status_code == 404:
            return "not_found_error"
        if status_code == 429:
            return "rate_limit_error"
        if status_code == 529:
            return "overloaded_error"
        return "api_error"

    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    return "api_error"


@dataclass
class ModelGatewayAPIError(Exception):
    """Model gateway exception rendered in the target client format.

    ``error_class`` / ``retry_after_seconds`` / ``terminal`` carry the shared
    upstream-error classification (see ``preloop.services.upstream_errors``)
    so callers can record the failure category (#118) and the HTTP layer can
    emit ``Retry-After`` / terminal-retry hints (#114, #116). They do not
    change the provider-native response body shape.
    """

    provider: GatewayProvider
    status_code: int
    message: str
    error_type: Optional[str] = None
    param: Optional[str] = None
    code: Optional[str] = None
    error_class: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    terminal: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if not self.error_type:
            self.error_type = _default_error_type(self.provider, self.status_code)

    def to_payload(self) -> dict[str, Any]:
        """Return the provider-native error response body."""
        if self.provider == "gemini":
            return {
                "error": {
                    "code": self.status_code,
                    "message": self.message,
                    "status": self.error_type,
                }
            }

        if self.provider == "anthropic":
            return {
                "type": "error",
                "error": {
                    "type": self.error_type,
                    "message": self.message,
                },
            }

        error_payload: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "param": self.param,
            "code": self.code,
        }
        return {"error": error_payload}

    def response_headers(self) -> dict[str, str]:
        """HTTP headers carrying retry/terminal hints for gateway clients."""
        headers: dict[str, str] = {}
        if self.retry_after_seconds is not None:
            headers["Retry-After"] = str(self.retry_after_seconds)
        if self.error_class:
            headers["X-Preloop-Error-Class"] = self.error_class
        if self.terminal:
            headers["X-Preloop-Retry-Terminal"] = "true"
        return headers
