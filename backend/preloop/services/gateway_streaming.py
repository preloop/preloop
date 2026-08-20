"""SSE streaming response that finishes the HTTP body before bookkeeping.

Starlette's ``StreamingResponse`` sends every iterator chunk with
``more_body=True``, then pulls the generator again. Code after a terminal
``yield`` therefore runs *before* the ASGI ``more_body=False`` frame, and
uvicorn can hold that last SSE event until the frame is sent. Measuring
scripts that stop at ``[DONE]`` then include usage recording in time-to-close.

``on_complete`` runs only after that empty final body frame.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from starlette.responses import StreamingResponse
from starlette.types import Send

logger = logging.getLogger(__name__)


class GatewayStreamingResponse(StreamingResponse):
    """``StreamingResponse`` that records usage after the body is finished."""

    def __init__(
        self,
        content: Any,
        *,
        on_complete: Optional[Callable[[], None]] = None,
        status_code: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        media_type: Optional[str] = None,
        background: Any = None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self.on_complete = on_complete

    async def stream_response(self, send: Send) -> None:
        """Send the SSE body, then run deferred usage recording."""
        await super().stream_response(send)
        if self.on_complete is None:
            return
        try:
            self.on_complete()
        except Exception:  # noqa: BLE001 - body is already on the wire
            logger.warning(
                "Deferred gateway stream recording failed after body flush",
                exc_info=True,
            )
