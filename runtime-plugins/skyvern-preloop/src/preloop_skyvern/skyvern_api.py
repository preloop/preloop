"""Minimal read client for the Skyvern REST API v1.

Pinned to the v1 task routes: ``GET /api/v1/tasks/{id}``,
``GET /api/v1/tasks/{id}/steps`` and
``GET /api/v1/tasks/{id}/steps/{step_id}/artifacts``, authenticated with
the ``x-api-key`` header. Skyvern serves these as legacy routes outside its
published OpenAPI, so responses are shape-checked and a mismatch fails
the import with a named ``SkyvernError`` instead of importing nothing.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.skyvern.com"


class SkyvernError(RuntimeError):
    """A Skyvern request failed; the message names the route and status."""


def _shape(what: str, expected: str) -> str:
    return (
        f"Skyvern {what} response is not {expected}; the v1 task routes this "
        "importer is pinned to may have changed"
    )


class SkyvernClient:
    """Read a task, its steps, step artifacts and artifact bytes."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key}
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def _get_json(self, path: str) -> Any:
        """GET an API route; every failure becomes ``SkyvernError``.

        Redirects are not followed here: the request carries ``x-api-key``,
        which httpx would forward to a redirect target.
        """
        url = f"{self.base_url}/api/v1{path}"
        try:
            response = self._client.get(
                url, headers=self._headers, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise SkyvernError(f"GET {path} failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SkyvernError(f"GET {path} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SkyvernError(f"GET {path} returned invalid JSON") from exc

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return the task response object."""
        task = self._get_json(f"/tasks/{task_id}")
        if not isinstance(task, dict) or task.get("task_id") != task_id:
            raise SkyvernError(_shape("task", "an object with the same task_id"))
        return task

    def get_steps(self, task_id: str) -> list[dict[str, Any]]:
        """Return the task's steps ordered by ``order`` then ``retry_index``."""
        steps = self._get_json(f"/tasks/{task_id}/steps")
        if not isinstance(steps, list) or not all(
            isinstance(s, dict) and s.get("step_id") for s in steps
        ):
            raise SkyvernError(_shape("steps", "a list of objects with step_id"))
        return sorted(steps, key=lambda s: (s.get("order", 0), s.get("retry_index", 0)))

    def get_step_artifacts(self, task_id: str, step_id: str) -> list[dict[str, Any]]:
        """Return the artifacts Skyvern stored for one step."""
        artifacts = self._get_json(f"/tasks/{task_id}/steps/{step_id}/artifacts")
        if not isinstance(artifacts, list):
            raise SkyvernError(_shape("artifacts", "a list"))
        return [a for a in artifacts if isinstance(a, dict)]

    def download(self, artifact: dict[str, Any], *, max_bytes: int) -> bytes | None:
        """Fetch an artifact's bytes from its signed URL.

        Returns ``None`` when there is no HTTP(S) URL, the fetch fails, or
        the body is larger than ``max_bytes`` (checked while streaming).
        Signed URLs are pre-authorized, so the API key is not sent to them.
        """
        url = artifact.get("signed_url") or artifact.get("uri") or ""
        if not url.startswith(("https://", "http://")):
            return None
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            return None
