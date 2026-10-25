"""Browser Use ``on_step_end`` callback that reports to Preloop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

from preloop_browser_use.client import BatchResult, PreloopTarget, StepPoster
from preloop_browser_use.convert import (
    history_item_to_step,
    resolve_screenshot_path,
    history_started_at,
)

logger = logging.getLogger("preloop_browser_use")


class PreloopBrowserUseReporter:
    """Queue each finished step and post batches in the background.

    ``on_step_end`` only converts and queues; posting happens in a worker
    thread, one batch at a time, so the agent loop never waits on Preloop.
    Call ``flush()`` (or use ``run()``) at the end of the run.

    Args:
        target: Preloop URL, key and session. ``None`` disables reporting.
        run_id: Stable run id for idempotency keys. Defaults to the agent's
            ``id`` or ``task_id``, else a random UUID.
        batch_size: Steps per batch, 1 to 200.
        client: Optional ``httpx.Client`` for tests.
        poster: Optional prebuilt ``StepPoster``.
    """

    def __init__(
        self,
        target: PreloopTarget | None,
        *,
        run_id: str | None = None,
        batch_size: int = 10,
        client: httpx.Client | None = None,
        poster: StepPoster | None = None,
    ) -> None:
        self.enabled = target is not None or poster is not None
        self._poster = poster or (
            StepPoster(target, client=client) if target is not None else None
        )
        self.run_id = run_id
        self.batch_size = min(max(1, batch_size), 200)
        self._queue: list[dict[str, Any]] = []
        self._seen = 0
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.results: list[BatchResult] = []
        self._started_at: datetime | None = None
        self._steps: list[dict[str, Any]] = []
        self.recording_uploaded = False

    @classmethod
    def from_env(cls, **kwargs: Any) -> PreloopBrowserUseReporter:
        """Build from ``PRELOOP_URL``, ``PRELOOP_AGENT_KEY`` and
        ``PRELOOP_RUNTIME_SESSION_ID``; disabled with one warning if unset."""
        target = PreloopTarget.from_env()
        if target is None:
            logger.warning(
                "Preloop reporting disabled: set PRELOOP_URL, PRELOOP_AGENT_KEY "
                "and PRELOOP_RUNTIME_SESSION_ID"
            )
        return cls(target, **kwargs)

    def _resolve_run_id(self, agent: Any) -> str:
        if self.run_id is None:
            for attr in ("id", "task_id"):
                value = getattr(agent, attr, None)
                if isinstance(value, str) and value:
                    self.run_id = value
                    break
            else:
                self.run_id = str(uuid.uuid4())
        return self.run_id

    @staticmethod
    def _history_items(agent: Any) -> list[Any]:
        history = getattr(agent, "history", None)
        if history is None:
            history = getattr(getattr(agent, "state", None), "history", None)
        items = getattr(history, "history", history)
        return list(items or [])

    async def on_step_end(self, agent: Any) -> None:
        """Browser Use hook: queue the steps finished since the last call."""
        if not self.enabled:
            return
        self._catch_up(agent)
        if len(self._queue) >= self.batch_size:
            self._schedule()

    def _catch_up(self, agent: Any) -> None:
        try:
            run_id = self._resolve_run_id(agent)
            items = self._history_items(agent)
            for index in range(self._seen, len(items)):
                observed = datetime.now(timezone.utc)
                if index == 0:
                    self._started_at = history_started_at(items[index], observed)
                step = history_item_to_step(
                    items[index], run_id=run_id, index=index, read_files=False
                )
                step.setdefault("occurred_at", observed.isoformat())
                self._queue.append(step)
                self._steps.append(step)
            self._seen = len(items)
        except Exception:  # noqa: BLE001 - never break the agent
            logger.warning(
                "Preloop could not convert a Browser Use step", exc_info=True
            )

    def _schedule(self) -> None:
        batch, self._queue = self._queue, []
        task = asyncio.get_running_loop().create_task(self._send(batch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, batch: list[dict[str, Any]]) -> None:
        if not batch or self._poster is None:
            return
        async with self._lock:
            try:
                self.results.extend(await asyncio.to_thread(self._post_batch, batch))
            except Exception:  # noqa: BLE001 - never break the agent
                logger.warning("Preloop browser step batch dropped", exc_info=True)

    def _post_batch(self, batch: list[dict[str, Any]]) -> list[BatchResult]:
        """Worker thread: read deferred screenshot files, then post."""
        if self._poster is None:
            return []
        return self._poster.post([resolve_screenshot_path(step) for step in batch])

    def close(self) -> None:
        """Close the HTTP client the reporter created (not one passed in)."""
        if self._poster is not None:
            self._poster.close()

    async def flush(self) -> list[BatchResult]:
        """Post whatever is queued and wait for in-flight batches."""
        if self._queue:
            self._schedule()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        return self.results

    async def finish(
        self,
        agent: Any,
        *,
        gif: bool = True,
        recording_path: str | Path | None = None,
        media_timing: dict[str, Any] | None = None,
    ) -> list[BatchResult]:
        """Flush steps and optionally deposit a finished recording; never fail the run.

        For an external video/GIF, pass its actual media_timing. A history GIF
        is generated on a worker from the observed timestamps, without task or
        reasoning overlays. An unrelated video is never assigned guessed timing.
        """
        if not self.enabled:
            return self.results
        self._catch_up(agent)
        await self.flush()
        if self.recording_uploaded or self._poster is None:
            return self.results
        try:
            if recording_path is not None:
                self.recording_uploaded = await asyncio.to_thread(
                    self._poster.deposit_recording,
                    Path(recording_path),
                    run_id=self._resolve_run_id(agent),
                    timing=media_timing,
                )
            elif gif:
                with TemporaryDirectory(prefix="preloop-recording-") as directory:
                    from preloop_browser_use.recording import history_gif

                    path = Path(directory) / "browser-use.gif"
                    timing = await asyncio.to_thread(
                        history_gif, self._steps, self._started_at, path
                    )
                    if path.exists():
                        self.recording_uploaded = await asyncio.to_thread(
                            self._poster.deposit_recording,
                            path,
                            run_id=self._resolve_run_id(agent),
                            timing=timing,
                        )
        except Exception:  # noqa: BLE001 - observer must not break the agent
            logger.warning("Preloop could not create or upload the recording")
        return self.results

    async def run(self, agent: Any, *, gif: bool = False, **run_kwargs: Any) -> Any:
        """Run ``agent`` with this reporter as ``on_step_end``, flush, close.

        A caller-supplied ``on_step_end`` still runs, after the reporter.
        Use one reporter per run; build a new one for the next run.
        """
        user_hook = run_kwargs.pop("on_step_end", None)

        async def hook(a: Any) -> None:
            await self.on_step_end(a)
            if user_hook is not None:
                await user_hook(a)

        try:
            return await agent.run(on_step_end=hook, **run_kwargs)
        finally:
            await self.finish(agent, gif=gif)
            self.close()
