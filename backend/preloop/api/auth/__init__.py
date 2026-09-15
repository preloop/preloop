"""Authentication package for the API."""

from typing import Any

from preloop.api.auth.jwt import (
    get_current_active_user,
    get_current_user,
    get_current_active_user_optional,  # Added optional user getter
    get_user_from_token_if_valid,  # Added for WebSocket auth
    oauth2_scheme,
)

__all__ = [
    "get_current_user",
    "get_current_active_user",
    "oauth2_scheme",
    "get_current_active_user_optional",  # Added optional user getter
    "get_user_from_token_if_valid",  # Added for WebSocket auth
]


def __getattr__(name: str) -> Any:
    """Load the control-plane auth router only when a caller asks for it."""
    if name == "auth_router":
        from preloop.api.auth.router import router as auth_router

        return auth_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
