"""Notice streaming gateway responses that no client ever consumed.

Every streaming gateway endpoint calls the upstream model provider *before*
handing its SSE generator to the ASGI layer (see
``OpenAIGatewayService._prefetch_upstream_stream``): the first upstream chunk
is pulled eagerly so upstream failures become real HTTP errors instead of
empty ``200`` streams. From that moment the provider is generating — and
billing — regardless of what happens to the connection.

The gateway's usage accounting, however, lives inside the generator body: the
success record at the end of the happy path, and the ``finally`` that records
a client disconnect. A Python generator that is closed *before its first
``next()``* never enters its body, so **none** of those blocks run:

    >>> def gen():
    ...     try:
    ...         yield 1
    ...     finally:
    ...         print("finally")
    >>> g = gen(); g.close()      # prints nothing

That is exactly the shape of a proxy read-timeout in front of the gateway:
the upstream call happened, the proxy gave up waiting for the first byte and
dropped the connection, the ASGI layer never pulled a chunk, and the request
vanished without a usage row, a status code, or an error class. The product
then reports a clean bill of health for a request the user saw fail.

:class:`ObservedGatewayStream` closes that gap. It wraps the SSE iterator and
tracks whether anything was ever consumed, so an unconsumed stream can be
recorded on teardown. It deliberately does **not** replace the in-generator
accounting: once iteration starts, the wrapper stays silent and the
generator's own ``finally`` owns the outcome. That split is what keeps a
disconnect from being counted twice.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

logger = logging.getLogger(__name__)


class ObservedGatewayStream:
    """SSE iterator wrapper that reports streams nobody ever consumed.

    Exactly one of three things happens to a wrapped stream, and exactly one
    accounting record results:

    - **Consumed to completion** — the wrapped generator records its own
      success row; the wrapper stays silent.
    - **Consumed then dropped** — the wrapped generator's ``finally`` runs and
      records the client disconnect; the wrapper stays silent.
    - **Never consumed** — no generator code ran at all, so the wrapper calls
      ``on_abandoned`` once.

    This is an explicit iterator class rather than a wrapping generator
    because a generator would inherit the very problem it is meant to solve:
    closing a never-started generator does not run its ``finally``.

    Args:
        stream: The underlying SSE iterator (usually a generator).
        on_abandoned: Called at most once when the stream is torn down
            without a single item having been requested. Must not raise;
            it runs during teardown, where an exception would replace a
            silent-but-harmless disconnect with a hard failure.
        closes: Extra resources to close **only when the stream was never
            consumed**. Their cleanup normally lives in the wrapped
            generator's ``finally``, which does run once iteration has
            started; closing them here as well would double-close them.
            They would otherwise leak when that body never runs at all.
    """

    __slots__ = ("_stream", "_on_abandoned", "_closes", "_consumed", "_closed")

    def __init__(
        self,
        stream: Iterable[Any],
        *,
        on_abandoned: Optional[Callable[[], None]] = None,
        closes: Sequence[Any] = (),
    ) -> None:
        self._stream: Iterator[Any] = iter(stream)
        self._on_abandoned = on_abandoned
        self._closes = tuple(closes)
        self._consumed = False
        self._closed = False

    def __iter__(self) -> "ObservedGatewayStream":
        return self

    def __next__(self) -> Any:
        # Mark before delegating: a stream that raised on its first chunk was
        # still started, and its own error handling owns that outcome.
        self._consumed = True
        return next(self._stream)

    def close(self) -> None:
        """Tear the stream down, recording it when it was never consumed.

        Safe to call repeatedly; only the first call has any effect.
        """
        if self._closed:
            return
        self._closed = True

        abandoned = not self._consumed
        if abandoned and self._on_abandoned is not None:
            try:
                self._on_abandoned()
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.warning(
                    "Recording an abandoned gateway stream failed", exc_info=True
                )

        # ``_closes`` are owned by the wrapped generator's ``finally`` once it
        # has started; closing them again here would double-close an upstream
        # HTTP response. Only take ownership when that body never ran.
        extra = self._closes if abandoned else ()
        for resource in (self._stream, *extra):
            closer = getattr(resource, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.warning(
                    "Closing a gateway stream resource failed", exc_info=True
                )

    def __del__(self) -> None:
        # The ASGI server does not always close the iterator explicitly; it
        # may simply drop its reference when the response task ends. Under
        # CPython refcounting that lands here, which is the only remaining
        # chance to account for an abandoned stream.
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from __del__
            pass
