"""SSE streaming response that finishes the HTTP body before bookkeeping.

Starlette's ``StreamingResponse`` sends every iterator chunk with
``more_body=True``, then pulls the generator again. Code after a terminal
``yield`` therefore runs *before* the ASGI ``more_body=False`` frame, and
uvicorn can hold that last SSE event until the frame is sent. Measuring
scripts that stop at ``[DONE]`` then include usage recording in time-to-close.

Success bookkeeping runs after that empty final body frame. Disconnect cleanup
also closes the source iterator and flushes any already-stashed record.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from typing import Any, Callable, Mapping, Optional

from anyio import CancelScope
from starlette.responses import StreamingResponse
from starlette.types import Send

from preloop.api.loop_safety import run_db_off_loop

logger = logging.getLogger(__name__)


async def _iterate_gateway_stream(iterator: Iterator[Any]) -> AsyncIterator[Any]:
    """Drain an in-flight sync pull before response cleanup can close it."""
    sentinel = object()

    def pull() -> Any:
        return next(iterator, sentinel)

    while True:
        item = await run_db_off_loop(pull)
        if item is sentinel:
            return
        yield item


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
        self._gateway_content = content
        if not isinstance(content, AsyncIterable):
            self._gateway_content = iter(content)
            self.body_iterator = _iterate_gateway_stream(self._gateway_content)

    async def stream_response(self, send: Send) -> None:
        """Send the SSE body, then run deferred usage recording."""
        try:
            await super().stream_response(send)
        finally:
            callback = self.on_complete
            self.on_complete = None
            content = self._gateway_content
            self._gateway_content = None

            def close_and_record() -> None:
                try:
                    close = getattr(content, "close", None)
                    if close is not None:
                        close()
                finally:
                    if callback is not None:
                        callback()

            if callback is not None or content is not None:
                try:
                    # ASGI <2.4 listens for disconnect concurrently. A client
                    # closing immediately after the final body can cancel us
                    # before the worker starts. Complete the stashed record
                    # before dependency cleanup may close its owned session.
                    # Closing the source iterator deterministically invokes
                    # observer abort accounting on incomplete streams. Every
                    # in-flight pull has drained before this cleanup runs.
                    with CancelScope(shield=True):
                        await run_db_off_loop(close_and_record)
                except Exception:  # noqa: BLE001 - body is already on the wire
                    logger.warning(
                        "Deferred gateway stream recording failed after body flush",
                        exc_info=True,
                    )
