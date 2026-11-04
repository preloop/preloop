"""Post browser step batches to Preloop with bounded retries.

Every failure is swallowed and logged: a Preloop outage must never stop
or slow the agent that is being observed.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("preloop_browser_use")

ENV_URL = "PRELOOP_URL"
ENV_AGENT_KEY = "PRELOOP_AGENT_KEY"
ENV_API_KEY = "PRELOOP_API_KEY"
ENV_SESSION_ID = "PRELOOP_RUNTIME_SESSION_ID"
#: The key the agent sends the gateway as ``X-Preloop-Session-Id``. When the
#: runtime session id is unset, it is resolved from this key (#1358).
ENV_CLIENT_SESSION_ID = "PRELOOP_SESSION_ID"
#: Gateway response header naming the attributed runtime session.
RUNTIME_SESSION_HEADER = "X-Preloop-Runtime-Session-Id"

MAX_BATCH = 200
#: Status codes worth retrying; anything else in 4xx is a caller error.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Per-row errors caused by the image alone; the step is re-sent without it.
IMAGE_ERRORS = frozenset({"screenshot_too_large", "screenshot_invalid"})


@dataclass(frozen=True)
class PreloopTarget:
    """Where steps go: base URL, agent key and runtime session id."""

    base_url: str
    agent_key: str
    runtime_session_id: str

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> PreloopTarget | None:
        """Read ``PRELOOP_URL``, ``PRELOOP_AGENT_KEY`` and the session id.

        ``PRELOOP_API_KEY`` is accepted when ``PRELOOP_AGENT_KEY`` is unset.
        ``PRELOOP_RUNTIME_SESSION_ID`` wins when set; otherwise the session
        is resolved (and created if needed) from ``PRELOOP_SESSION_ID``, the
        key the agent sends the gateway, so steps and model turns share one
        session. Returns ``None`` when the target cannot be determined so
        the caller can run the agent without reporting.

        Args:
            env: Environment mapping; defaults to ``os.environ``.
            client: Optional ``httpx.Client`` for the resolve call (tests).
        """
        env = os.environ if env is None else env
        url = (env.get(ENV_URL) or "").strip()
        key = (env.get(ENV_AGENT_KEY) or env.get(ENV_API_KEY) or "").strip()
        sid = (env.get(ENV_SESSION_ID) or "").strip()
        if url and key and not sid:
            client_sid = (env.get(ENV_CLIENT_SESSION_ID) or "").strip()
            if client_sid:
                sid = resolve_runtime_session_id(url, key, client_sid, client=client)
        if not (url and key and sid):
            return None
        return cls(base_url=url, agent_key=key, runtime_session_id=sid)

    @property
    def steps_url(self) -> str:
        """Browser step batch route for this session."""
        base = _api_base(self.base_url)
        return f"{base}/runtime-sessions/{self.runtime_session_id}/browser-steps"


def _api_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base


def resolve_runtime_session_id(
    base_url: str,
    agent_key: str,
    client_session_id: str,
    *,
    client: httpx.Client | None = None,
    create: bool = True,
    timeout: float = 3.0,
) -> str:
    """Resolve the gateway session key to a runtime session id.

    Calls ``GET /api/v1/runtime-sessions/resolve`` with the same key the
    agent sends the gateway as ``X-Preloop-Session-Id``. Never raises.

    Args:
        base_url: Preloop base URL, with or without ``/api/v1``.
        agent_key: Bearer the gateway accepts.
        client_session_id: The gateway session key.
        client: Optional ``httpx.Client`` (tests).
        create: Create the session as the gateway would when missing.
        timeout: Request timeout in seconds. Short on purpose: this runs
            before the agent starts, and an unreachable Preloop must not
            hold it up.

    Returns:
        The runtime session id, or ``""`` when it could not be resolved.
    """
    owns = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.get(
            f"{_api_base(base_url)}/runtime-sessions/resolve",
            params={
                "client_session_id": client_session_id,
                "create": "true" if create else "false",
            },
            headers={"Authorization": f"Bearer {agent_key}"},
        )
        if response.status_code != 200:
            logger.warning(
                "Preloop session resolve returned HTTP %s", response.status_code
            )
            return ""
        value = response.json().get("runtime_session_id")
        return value if isinstance(value, str) else ""
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("Preloop session resolve failed: %s", type(exc).__name__)
        return ""
    finally:
        if owns:
            http.close()


class RuntimeSessionCapture:
    """``httpx`` response hook that records the gateway's session header.

    Register it on the LLM client that talks to the Preloop gateway
    (``httpx.Client(event_hooks={"response": [capture]})``); after the first
    model call, :meth:`target` returns where steps should go.
    """

    def __init__(self) -> None:
        self.runtime_session_id: str | None = None

    def __call__(self, response: httpx.Response) -> None:
        value = response.headers.get(RUNTIME_SESSION_HEADER)
        if value:
            self.runtime_session_id = value.strip()

    def target(self, base_url: str, agent_key: str) -> PreloopTarget | None:
        """The step target once a session header was seen, else ``None``."""
        if not self.runtime_session_id:
            return None
        return PreloopTarget(base_url, agent_key, self.runtime_session_id)


@dataclass
class BatchResult:
    """Outcome of one batch post; ``ok`` is false when it was dropped."""

    ok: bool
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] | None = None


class StepPoster:
    """Synchronous poster; async adapters run it in a worker thread.

    Args:
        target: Preloop URL, key and session.
        client: Optional ``httpx.Client`` (tests pass a mock transport or
            the FastAPI test client).
        attempts: Total tries per batch.
        backoff: First retry delay in seconds; doubles each retry.
        timeout: Per-request timeout in seconds.
        sleep: Injected for tests.
    """

    def __init__(
        self,
        target: PreloopTarget,
        *,
        client: httpx.Client | None = None,
        attempts: int = 3,
        backoff: float = 0.5,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = target
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._sleep = sleep

    def post(self, steps: Sequence[dict[str, Any]]) -> list[BatchResult]:
        """Post ``steps`` in chunks of at most 200; never raises."""
        results = []
        for start in range(0, len(steps), MAX_BATCH):
            results.append(self._post_one(list(steps[start : start + MAX_BATCH])))
        return results

    def _settle(
        self, steps: list[dict[str, Any]], body: dict[str, Any], *, retry_images: bool
    ) -> BatchResult:
        """Count a 200 response; re-post image-rejected rows without images.

        The ingest route refuses the whole row when its screenshot is too
        large or invalid. Those steps are sent again once without the image
        so the step itself still reaches the timeline.
        """
        result = BatchResult(
            ok=True,
            accepted=int(body.get("accepted", 0)),
            duplicates=int(body.get("duplicates", 0)),
            rejected=[],
        )
        retry: list[dict[str, Any]] = []
        origin: list[int] = []  # batch index of each retried step
        for row in body.get("rejected") or []:
            index, error = row.get("index"), row.get("error")
            if (
                retry_images
                and error in IMAGE_ERRORS
                and isinstance(index, int)
                and 0 <= index < len(steps)
                and steps[index].get("screenshot")
            ):
                step = {k: v for k, v in steps[index].items() if k != "screenshot"}
                step["extra"] = {**step.get("extra", {}), "screenshot_omitted": error}
                retry.append(step)
                origin.append(index)
                continue
            logger.warning("Preloop refused browser step %s", row)
            result.rejected.append(row)
        if retry:
            logger.warning(
                "Preloop refused %d screenshot(s); sending those steps without them",
                len(retry),
            )
            again = self._post_one(retry, retry_images=False)
            result.ok = result.ok and again.ok
            result.accepted += again.accepted
            result.duplicates += again.duplicates
            for row in again.rejected or []:
                sub = row.get("index")
                ok = isinstance(sub, int) and 0 <= sub < len(origin)
                result.rejected.append({**row, "index": origin[sub] if ok else 0})
        return result

    def _post_one(
        self, steps: list[dict[str, Any]], *, retry_images: bool = True
    ) -> BatchResult:
        headers = {"Authorization": f"Bearer {self.target.agent_key}"}
        last_error = "unknown error"
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                response = self._client.post(
                    self.target.steps_url, json={"steps": steps}, headers=headers
                )
            except Exception as exc:  # noqa: BLE001 - never break the agent
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 200:
                return self._settle(steps, response.json(), retry_images=retry_images)
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                break
        logger.warning(
            "Preloop browser step batch of %d dropped after %s: %s",
            len(steps),
            "retries" if self.attempts > 1 else "one try",
            last_error,
        )
        return BatchResult(ok=False)

    def deposit_recording(
        self, path: Path, *, run_id: str, timing: dict[str, Any] | None
    ) -> bool:
        """Upload a finished file with an idempotent identity and bounded retries."""
        content_type = {
            ".gif": "image/gif",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
        }.get(path.suffix.lower())
        if content_type is None:
            logger.warning("Preloop recording must be GIF, MP4 or WebM")
            return False
        kind = "screencast" if content_type == "image/gif" else "recording"
        cap = (64 if kind == "screencast" else 512) * 1024 * 1024
        metadata: dict[str, Any] = {
            "kind": kind,
            "name": path.name,
            "labels": {"tags": ["browser_use"]},
        }
        if timing is not None:
            metadata["media_timing"] = timing
        headers = {
            "Authorization": f"Bearer {self.target.agent_key}",
            "Idempotency-Key": f"browser_use:{run_id}:recording:0",
        }
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            try:
                if path.stat().st_size > cap:
                    logger.warning("Preloop recording exceeds the artifact size cap")
                    return False
                # Reopen on retry: a consumed multipart stream cannot be replayed.
                with path.open("rb") as stream:
                    response = self._client.post(
                        self.target.steps_url.rsplit("/", 1)[0] + "/artifacts",
                        headers=headers,
                        files={"file": (path.name, stream, content_type)},
                        data={"metadata": json.dumps(metadata)},
                    )
                if response.status_code == 201:
                    return True
                if response.status_code not in RETRYABLE_STATUS:
                    break
            except (OSError, httpx.HTTPError):
                continue
        logger.warning("Preloop recording upload failed after bounded retries")
        return False

    def close(self) -> None:
        """Close the HTTP client when this poster created it."""
        if self._owns_client:
            self._client.close()
