"""Write browser steps and artifacts to a Preloop runtime session."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("preloop_skyvern")

MAX_BATCH = 200
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Per-row errors caused by the image alone; the step is re-sent without it.
IMAGE_ERRORS = frozenset({"screenshot_too_large", "screenshot_invalid"})


@dataclass(frozen=True)
class PreloopTarget:
    """Preloop base URL, agent key and runtime session id."""

    base_url: str
    agent_key: str
    runtime_session_id: str

    def url(self, suffix: str) -> str:
        """Session route under ``/api/v1`` (base may already include it)."""
        base = self.base_url.rstrip("/")
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        return f"{base}/runtime-sessions/{self.runtime_session_id}/{suffix}"


@dataclass
class StepTotals:
    """Summed batch results."""

    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    failed_batches: int = 0


class DepositUnavailableError(RuntimeError):
    """The Preloop server has no artifact deposit route (before #1080)."""


class PreloopClient:
    """Retrying writer for the browser step and artifact deposit routes."""

    def __init__(
        self,
        target: PreloopTarget,
        *,
        client: httpx.Client | None = None,
        attempts: int = 3,
        backoff: float = 0.5,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = target
        self._client = client or httpx.Client(timeout=timeout)
        self._auth = {"Authorization": f"Bearer {target.agent_key}"}
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._sleep = sleep

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        headers = {**self._auth, **kwargs.pop("headers", {})}
        last: httpx.Response | None = None
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                last = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                logger.debug("Preloop request failed: %s", exc)
                last = None
                continue
            if last.status_code not in RETRYABLE_STATUS:
                return last
        return last

    def post_steps(self, steps: list[dict[str, Any]]) -> StepTotals:
        """Post steps in batches of at most 200."""
        totals = StepTotals()
        for start in range(0, len(steps), MAX_BATCH):
            batch = steps[start : start + MAX_BATCH]
            response = self._send(
                "POST", self.target.url("browser-steps"), json={"steps": batch}
            )
            if response is None or response.status_code != 200:
                totals.failed_batches += 1
                status = "no response" if response is None else response.status_code
                logger.warning(
                    "Preloop refused a batch of %d Skyvern steps: %s",
                    len(batch),
                    status,
                )
                continue
            self._settle(batch, response.json(), start, totals)
        return totals

    def _settle(
        self,
        batch: list[dict[str, Any]],
        body: dict[str, Any],
        start: int,
        totals: StepTotals,
    ) -> None:
        """Count a batch; re-send rows refused for their image without it."""
        totals.accepted += int(body.get("accepted", 0))
        totals.duplicates += int(body.get("duplicates", 0))
        retry = []
        for row in body.get("rejected") or []:
            index = row.get("index")
            if (
                row.get("error") in IMAGE_ERRORS
                and isinstance(index, int)
                and 0 <= index < len(batch)
                and batch[index].get("screenshot")
            ):
                step = {k: v for k, v in batch[index].items() if k != "screenshot"}
                step["extra"] = {
                    **step.get("extra", {}),
                    "screenshot_omitted": row["error"],
                }
                retry.append(step)
                continue
            logger.warning("Preloop refused a Skyvern step: %s", row)
            totals.rejected.append({**row, "index": (index or 0) + start})
        if not retry:
            return
        logger.warning(
            "Preloop refused %d screenshot(s); sending those steps without them",
            len(retry),
        )
        response = self._send(
            "POST", self.target.url("browser-steps"), json={"steps": retry}
        )
        if response is None or response.status_code != 200:
            totals.failed_batches += 1
            return
        again = response.json()
        totals.accepted += int(again.get("accepted", 0))
        totals.duplicates += int(again.get("duplicates", 0))
        for row in again.get("rejected") or []:
            logger.warning("Preloop refused a Skyvern step: %s", row)
            totals.rejected.append(row)

    def deposit(
        self,
        *,
        kind: str,
        name: str,
        content_type: str,
        data: bytes,
        idempotency_key: str,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Deposit one artifact as multipart; return the descriptor or None.

        Raises:
            DepositUnavailableError: the route does not exist on this server.
        """
        metadata = {"kind": kind, "name": name, "labels": labels or {}}
        response = self._send(
            "POST",
            self.target.url("artifacts"),
            headers={"Idempotency-Key": idempotency_key},
            files={"file": (name, data, content_type)},
            data={"metadata": json.dumps(metadata)},
        )
        if response is None:
            logger.warning("Preloop did not answer the %s deposit for %s", kind, name)
            return None
        if response.status_code == 405 or (
            response.status_code == 404 and _is_route_missing(response)
        ):
            raise DepositUnavailableError(name)
        if response.status_code != 201:
            logger.warning(
                "Preloop refused the %s artifact %s: HTTP %s %s",
                kind,
                name,
                response.status_code,
                _detail(response),
            )
            return None
        return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))
    except ValueError:
        return ""


def _is_route_missing(response: httpx.Response) -> bool:
    """FastAPI answers an unknown route with ``{"detail": "Not Found"}``.

    A missing session on a server that has the route answers
    ``runtime_session_not_found`` or similar, which is a real error.
    """
    return _detail(response) == "Not Found"
