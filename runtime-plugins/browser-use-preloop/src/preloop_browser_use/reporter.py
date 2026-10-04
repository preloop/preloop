"""Browser Use ``on_step_end`` callback that reports to Preloop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from preloop_browser_use.client import BatchResult, PreloopTarget, StepPoster
from preloop_browser_use.convert import history_item_to_step, resolve_screenshot_path

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
                self._queue.append(
                    history_item_to_step(
                        items[index], run_id=run_id, index=index, read_files=False
                    )
                )
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

    async def run(self, agent: Any, **run_kwargs: Any) -> Any:
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
            if self.enabled:
                self._catch_up(agent)
            await self.flush()
            self.close()
