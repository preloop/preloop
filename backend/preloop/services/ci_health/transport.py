"""Read-only HTTP access to CI providers with a request budget.

The transport only issues ``GET``. Every response is checked against the
provider's remaining-quota header; once it falls under the floor, or the
provider answers 429 / a rate-limited 403, the budget stops further requests
and the caller reports partial data instead of looping. Log downloads stream
with a byte cap and keep the tail, where test summaries are printed.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Hashable, Optional, TypeVar

import httpx

#: Default floor of remaining provider requests below which reading stops.
DEFAULT_RATE_LIMIT_FLOOR = 300
#: Bytes of a job log streamed at most, and the tail kept from them.
LOG_MAX_STREAM_BYTES = 8 * 1024 * 1024
LOG_TAIL_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0

HeadersProvider = Callable[[], Awaitable[dict[str, str]]]
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class BudgetExhaustedError(Exception):
    """No more requests may be made; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        """Store the reason.

        Args:
            reason: ``request_budget``, ``rate_limited`` or ``rate_limit_floor``.
        """
        super().__init__(reason)
        self.reason = reason


class ProviderError(Exception):
    """A provider answered with an error the collector cannot work around."""

    def __init__(self, status: int, message: str) -> None:
        """Store the status.

        Args:
            status: HTTP status (0 for a connection failure).
            message: Short description without response bodies.
        """
        super().__init__(message)
        self.status = status


@dataclass
class Budget:
    """Requests left for one collector call, shared across repositories."""

    max_requests: int
    floor: int = DEFAULT_RATE_LIMIT_FLOOR
    used: int = 0
    remaining: Optional[int] = None
    stopped: Optional[str] = None

    def take(self) -> None:
        """Account for one request about to be made.

        Raises:
            BudgetExhaustedError: when the budget or the provider quota is spent.
        """
        if self.stopped:
            raise BudgetExhaustedError(self.stopped)
        if self.used >= self.max_requests:
            self.stopped = "request_budget"
            raise BudgetExhaustedError(self.stopped)
        if self.remaining is not None and self.remaining < self.floor:
            self.stopped = "rate_limit_floor"
            raise BudgetExhaustedError(self.stopped)
        self.used += 1

    def observe(self, headers: httpx.Headers, status: int) -> None:
        """Read quota headers and stop on a rate-limit answer.

        Args:
            headers: Response headers.
            status: Response status.

        Raises:
            BudgetExhaustedError: when the provider says to stop.
        """
        raw = headers.get("x-ratelimit-remaining") or headers.get("ratelimit-remaining")
        if raw is not None:
            try:
                value = int(raw)
            except ValueError:
                value = None
            if value is not None:
                self.remaining = (
                    value if self.remaining is None else min(self.remaining, value)
                )
        limited = status == 429 or (
            status == 403
            and (headers.get("retry-after") is not None or self.remaining == 0)
        )
        if limited:
            self.stopped = "rate_limited"
            raise BudgetExhaustedError(self.stopped)


@dataclass
class Response:
    """A decoded response."""

    status: int
    body: Any
    headers: httpx.Headers


class HttpTransport:
    """GET-only client bound to one provider base URL and credential."""

    def __init__(
        self,
        base_url: str,
        headers: HeadersProvider,
        budget: Budget,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Bind the transport.

        Args:
            base_url: API root, for example ``https://api.github.com``.
            headers: Coroutine returning auth headers (refreshed per call so
                short-lived installation tokens keep working).
            budget: Shared request budget.
            client: Optional client (tests pass one with a mock transport).
        """
        self.base_url = base_url.rstrip("/")
        self._headers = headers
        self.budget = budget
        self._client = client

    async def _send(
        self, path: str, params: Optional[dict[str, Any]], *, stream_text: bool
    ) -> Response:
        self.budget.take()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = await self._headers()
        client = self._client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            if stream_text:
                # httpx drops Authorization on a cross-origin redirect, which
                # is what GitHub's log endpoint answers with.
                async with client.stream(
                    "GET", url, params=params, headers=headers, follow_redirects=True
                ) as response:
                    self.budget.observe(response.headers, response.status_code)
                    if response.status_code >= 400:
                        return Response(response.status_code, None, response.headers)
                    tail = bytearray()
                    read = 0
                    async for chunk in response.aiter_bytes():
                        read += len(chunk)
                        tail.extend(chunk)
                        if len(tail) > LOG_TAIL_BYTES:
                            del tail[: len(tail) - LOG_TAIL_BYTES]
                        if read >= LOG_MAX_STREAM_BYTES:
                            break
                    text = tail.decode("utf-8", errors="replace")
                    return Response(response.status_code, text, response.headers)
            response = await client.get(url, params=params, headers=headers)
            self.budget.observe(response.headers, response.status_code)
            if response.status_code >= 400:
                return Response(response.status_code, None, response.headers)
            try:
                body = response.json()
            except ValueError:
                body = None
            return Response(response.status_code, body, response.headers)
        except httpx.HTTPError as exc:
            raise ProviderError(0, f"connection error: {type(exc).__name__}") from None
        finally:
            if self._client is None:
                await client.aclose()

    async def get_json(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> Response:
        """GET a JSON document.

        Args:
            path: Path under the base URL (or an absolute URL).
            params: Query parameters.

        Returns:
            The response; ``body`` is None for an error status.
        """
        return await self._send(path, params, stream_text=False)

    async def get_text_tail(self, path: str) -> Response:
        """GET a text document, keeping at most its last 256 KB.

        Args:
            path: Path under the base URL.

        Returns:
            The response; ``body`` is the text tail or None.
        """
        return await self._send(path, None, stream_text=True)


class LruCache(Generic[K, V]):
    """Small in-process LRU for immutable provider records."""

    def __init__(self, size: int) -> None:
        """Create the cache.

        Args:
            size: Entries kept.
        """
        self.size = size
        self._data: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> Optional[V]:
        """Return a cached value and mark it recent."""
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: K, value: V) -> None:
        """Store a value, evicting the oldest beyond the size."""
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """Drop everything (tests)."""
        self._data.clear()
