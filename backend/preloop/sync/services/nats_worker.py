"""
Preloop Sync NATS Worker
Subscribes to NATS messages and triggers tracker synchronization.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from collections.abc import AsyncIterator
import json
import logging
import os
import datetime
import inspect
import signal
import nats
import socket
import uuid
from typing import Any, List, Optional, Set, Tuple
from nats.aio.client import Client as NATSClient
from nats.aio.errors import ErrNoServers
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import APIError

import preloop.sync.tasks as tasks
from preloop.sync.config import logger

FLOW_ORCHESTRATION_TASKS = frozenset({"execute_flow", "resume_flow_execution"})


WEBHOOK_PROGRESS_INTERVAL_SECONDS = 60.0


@asynccontextmanager
async def _webhook_progress(msg: Any) -> AsyncIterator[None]:
    """Renew the 180-second lease while webhook embeddings run off-loop."""

    async def renew() -> None:
        while True:
            await asyncio.sleep(WEBHOOK_PROGRESS_INTERVAL_SECONDS)
            try:
                await msg.in_progress()
            except Exception:
                logger.warning("Failed to renew webhook task progress", exc_info=True)

    heartbeat = asyncio.create_task(renew())
    try:
        yield
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            if await heartbeat is not None:
                logger.debug("webhook progress heartbeat finished with a result")


class PreloopSyncNatsWorker:
    def __init__(
        self,
        nats_url: str,
        queue_name: str,
        tasks_allowlist: Optional[List[str]] = None,
        tasks_excludelist: Optional[List[str]] = None,
        max_inflight: Optional[int] = None,
    ):
        self.nats_url = nats_url
        self.queue_name = queue_name
        self.tasks_allowlist = tasks_allowlist or []
        self.tasks_excludelist = tasks_excludelist or []
        self._max_inflight_override = max_inflight
        self.nc: NATSClient = None
        self.js = None
        self.subs: List[Tuple[str, nats.aio.client.Subscription]] = []
        self.connection_name = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        # When False, stop fetching new JetStream messages (deploy drain).
        self._accepting = True
        self._inflight: Set[asyncio.Task] = set()
        self._run_slots: Optional[asyncio.Semaphore] = None
        self._reclaim_task: Optional[asyncio.Task] = None
        self._pull_tasks: List[asyncio.Task] = []

    async def _remove_conflicting_wildcard_consumer(
        self, subjects_to_subscribe: List[str]
    ) -> None:
        """Delete the legacy wildcard consumer when using filtered subjects.

        Upgrade path: before dedicated pools existed, the default worker
        subscribed to `preloop.sync.tasks.*` and left a DURABLE consumer on the
        stream. On a workqueue stream that wildcard overlaps every filtered
        consumer, so once a pool starts filtering, all of its subscriptions are
        rejected with `filtered consumer not unique on workqueue stream` and
        the worker cannot start. The stale consumer outlives the process, so
        upgrading alone would not clear it — remove it explicitly.

        No-op when this worker IS the wildcard subscriber.
        """
        wildcard_subject = "preloop.sync.tasks.*"
        if wildcard_subject in subjects_to_subscribe:
            return

        legacy_durable = f"{self.queue_name}_preloop-sync-tasks-all"
        try:
            await self.js.consumer_info("tasks", legacy_durable)
        except Exception:
            return  # not present: nothing to clean up

        try:
            await self.js.delete_consumer("tasks", legacy_durable)
            logger.warning(
                "Removed legacy wildcard consumer '%s': it overlaps the filtered "
                "consumers this worker needs (workqueue streams require "
                "non-overlapping subject filters).",
                legacy_durable,
            )
        except Exception:
            logger.exception(
                "Failed to remove legacy wildcard consumer '%s'; filtered "
                "subscriptions will fail until it is deleted.",
                legacy_durable,
            )

    def _subjects_to_subscribe(self) -> List[str]:
        """Return the NATS subjects this worker should consume.

        The ``tasks`` stream uses WORKQUEUE retention: consumer subject
        filters may not overlap. So a worker that EXCLUDES tasks (because a
        dedicated pool handles them) must enumerate the remaining subjects
        explicitly — subscribing to the `preloop.sync.tasks.*` wildcard would
        overlap the dedicated pool's filtered consumers and NATS would reject
        them with `filtered consumer not unique on workqueue stream`.
        """
        dispatchable_tasks = tasks.DISPATCHABLE_TASKS

        if self.tasks_allowlist:
            names = list(self.tasks_allowlist)
        elif self.tasks_excludelist:
            excluded = set(self.tasks_excludelist)
            names = [task for task in dispatchable_tasks if task not in excluded]
            unknown = excluded - set(dispatchable_tasks)
            if unknown:
                logger.warning(
                    "Ignoring unknown task(s) in exclude list: %s",
                    ", ".join(sorted(unknown)),
                )
        else:
            return ["preloop.sync.tasks.*"]

        return [f"preloop.sync.tasks.{name}" for name in names]

    @property
    def handles_flow_orchestration(self) -> bool:
        """True when this worker may run execute_flow / resume_flow_execution.

        Empty allowlists still return True so the default pool keeps the
        stale-claim reaper. That pool *excludes* the flow subjects, so it
        must not inherit flow-execution fan-out; see `_consumes_flow_tasks`.
        """
        if not self.tasks_allowlist:
            return True
        return bool(FLOW_ORCHESTRATION_TASKS.intersection(self.tasks_allowlist))

    def _consumes_flow_tasks(self) -> bool:
        """True when this process actually subscribes to execute/resume."""
        subjects = self._subjects_to_subscribe()
        if "preloop.sync.tasks.*" in subjects:
            return True
        flow = {f"preloop.sync.tasks.{t}" for t in FLOW_ORCHESTRATION_TASKS}
        return bool(flow.intersection(subjects))

    def handler_concurrency(self) -> int:
        """How many task handlers this process may run at once.

        Flow-execution workers babysit hosted agent Jobs. The monitor loop is
        wait-bound (poll/heartbeat/logs), so one process can own several
        claims. Other pools stay serial: webhook embedding and polling are
        heavier per message and were not sized for fan-out.

        Shared across every pull subscription on this process so
        execute_flow + resume_flow_execution cannot each take the cap.
        """
        if self._max_inflight_override is not None:
            return max(1, int(self._max_inflight_override))
        if not self._consumes_flow_tasks():
            return 1
        from preloop.config import settings

        raw = getattr(settings, "flow_execution_max_inflight", 10)
        try:
            return max(1, int(raw or 10))
        except (TypeError, ValueError):
            return 10

    async def connect(self):
        if self.nc and self.nc.is_connected:
            logger.info("NATS client already connected.")
            return

        logger.info(
            f"Worker '{self.connection_name}' connecting to NATS server at {self.nats_url}"
        )
        try:
            self.nc = await nats.connect(
                self.nats_url,
                name=self.connection_name,
            )
            self.js = self.nc.jetstream()
            logger.info(
                f"Worker '{self.connection_name}' successfully connected to NATS server: {self.nats_url}"
            )

            # Ensure the 'tasks' stream exists
            config = StreamConfig(
                name="tasks",
                subjects=["preloop.sync.tasks.*"],
                retention="workqueue",
                max_age=24 * 60 * 60,  # 24 hours in seconds
            )
            try:
                await self.js.stream_info("tasks")
            except APIError as e:
                if e.err_code == 10059:  # Stream not found
                    logger.info("Stream 'tasks' not found. Creating it...")
                    await self.js.add_stream(config)
                    logger.info("Stream 'tasks' created successfully.")
                else:
                    raise e

        except ErrNoServers as e:
            logger.error(
                f"Worker '{self.connection_name}' could not connect to NATS: No servers available at {self.nats_url}. Error: {e}"
            )
            self.nc = None
            raise
        except Exception as e:
            logger.error(
                f"Worker '{self.connection_name}' error connecting to NATS at {self.nats_url}: {e}"
            )
            self.nc = None
            raise

    async def start_listening(self):
        # Worker tasks (e.g. usage repricing) estimate model costs; load the
        # vendored price catalog so they price identically to the API pods.
        try:
            from preloop.services.model_price_catalog import load_catalog

            load_catalog()
        except Exception:
            logger.exception("Model price catalog load failed; using litellm defaults")

        if not self.nc or not self.nc.is_connected:
            await self.connect()

        if not self.nc:
            logger.error("Cannot start listening, NATS client not connected.")
            return

        from preloop.services.reviewed_model_price_refresh import (
            start_reviewed_price_refresh,
        )

        if getattr(self, "_price_refresher", None) is None:
            self._price_refresher = start_reviewed_price_refresh()

        from preloop.services.model_price_catalog import start_price_map_refresh

        if getattr(self, "_price_map_refresher", None) is None:
            self._price_map_refresher = start_price_map_refresh()

        subjects_to_subscribe = self._subjects_to_subscribe()

        logger.info(
            f"Worker '{self.connection_name}' subscribing to subjects: {subjects_to_subscribe}"
        )

        await self._remove_conflicting_wildcard_consumer(subjects_to_subscribe)

        for subject in subjects_to_subscribe:
            try:
                # For a durable, filtered consumer, the durable name must be unique
                # for each subject filter. We construct it from the queue name
                # and the task name (the last part of the subject).
                sanitized_subject = subject.replace(".", "-").replace("*", "all")
                durable_name = f"{self.queue_name}_{sanitized_subject}"

                # 1. Explicitly create or update the consumer. This is an
                # idempotent operation. The `deliver_group` makes this a queue
                # consumer on the server side, ensuring messages are load-balanced.
                consumer_config = ConsumerConfig(
                    durable_name=durable_name,
                    ack_wait=180,  # 3 minutes
                    filter_subject=subject,
                    deliver_group=self.queue_name,
                )
                await self.js.add_consumer(stream="tasks", config=consumer_config)

                # 2. Create a pull subscription to the durable queue consumer.
                # This allows the worker to fetch messages from the shared consumer.
                sub = await self.js.pull_subscribe(
                    subject=subject,
                    durable=durable_name,
                )
                self.subs.append((subject, sub))
            except Exception as e:
                logger.error(f"Failed to subscribe to NATS subject '{subject}': {e}")
                raise

        logger.info(f"Worker '{self.connection_name}' is now listening for messages.")

        async def message_handler(msg):
            subject = msg.subject
            data = msg.data.decode()
            logger.info(f"Received message on '{subject}': {data}")

            start_time = datetime.datetime.now()
            acked = False

            try:
                payload = json.loads(data)
                task_name = payload.get("function")

                if not task_name:
                    logger.error(f"Unknown message format: {data}")
                    if os.getenv("SENTRY_DSN"):
                        import sentry_sdk

                        sentry_sdk.capture_exception(
                            Exception(f"Unknown message format: {data}")
                        )
                    await msg.ack()
                    acked = True
                    return

                try:
                    func = getattr(tasks, task_name)
                except AttributeError:
                    logger.error(f"Task function not found: '{task_name}'")
                    if os.getenv("SENTRY_DSN"):
                        import sentry_sdk

                        sentry_sdk.capture_exception(
                            AttributeError(f"Task function not found: '{task_name}'")
                        )
                    await msg.ack()
                    acked = True
                    return

                # Draining: do not start new long-running orchestration; nak so
                # another worker (or this worker after restart) can take it.
                if not self._accepting and task_name in FLOW_ORCHESTRATION_TASKS:
                    try:
                        await msg.nak()
                    except Exception:  # noqa: BLE001 - drain nak is best-effort
                        logger.warning(
                            "Failed to nak %s while draining; continuing drain",
                            task_name,
                            exc_info=True,
                        )
                    logger.info(
                        "Draining: nacked %s so another worker can adopt it",
                        task_name,
                    )
                    return

                async def early_ack() -> None:
                    nonlocal acked
                    if not acked:
                        await msg.ack()
                        acked = True
                        logger.info(
                            "Acknowledged task '%s' early: the handler "
                            "reported its durable work is committed",
                            task_name,
                        )

                async def delayed_nak(delay: float = 0) -> None:
                    """Return the message to the stream instead of acking it.

                    A handler that refused the work on purpose (an account is
                    at its concurrency cap) must not drop the message: the
                    execution still has to run, just not now and not
                    necessarily here.
                    """
                    nonlocal acked
                    if acked:
                        return
                    await msg.nak(delay=delay)
                    acked = True
                    logger.info(
                        "Task '%s' returned to the stream for redelivery in %ss",
                        task_name,
                        delay,
                    )

                call_kwargs = dict(payload.get("kwargs", {}) or {})
                # ack-after-claim (flow orchestration) and ack-after-commit
                # (webhook fan-out) both hand the handler an ack callable so
                # it decides when redelivery stops being useful.
                if task_name in getattr(
                    tasks, "ACK_AFTER_CLAIM_TASKS", ()
                ) or task_name in getattr(tasks, "ACK_AFTER_COMMIT_TASKS", ()):
                    call_kwargs["_ack"] = early_ack
                if task_name in getattr(tasks, "ACK_AFTER_CLAIM_TASKS", ()):
                    call_kwargs["_nak"] = delayed_nak

                if task_name == "process_webhook_event" and call_kwargs.get(
                    "embedding_requests"
                ):
                    # Generation drains its worker thread on cancellation; keep
                    # renewing until that drain finishes, then ack/nak below.
                    async with _webhook_progress(msg):
                        stats = await func(*payload.get("args", []), **call_kwargs)
                elif task_name == "reprice_gateway_usage_task":
                    from preloop.api.loop_safety import run_db_off_loop

                    async with _webhook_progress(msg):
                        stats = await run_db_off_loop(
                            lambda: func(*payload.get("args", []), **call_kwargs)
                        )
                elif inspect.iscoroutinefunction(func):
                    stats = await func(*payload.get("args", []), **call_kwargs)
                else:
                    stats = func(*payload.get("args", []), **call_kwargs)

                if not acked:
                    await msg.ack()
                    acked = True

                end_time = datetime.datetime.now()
                logger.info(
                    f"Task '{task_name}' completed and acknowledged. Stats: {stats}. Duration: {end_time - start_time}"
                )

            except asyncio.CancelledError:
                # Deploy drain cancelled an in-flight handler. Prefer nak if we
                # never claimed/acked so the message can be redelivered.
                if not acked:
                    try:
                        await msg.nak()
                    except Exception:  # noqa: BLE001 - cancel-path nak is best-effort
                        logger.warning(
                            "Failed to nak cancelled task before re-raise",
                            exc_info=True,
                        )
                raise
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON payload: {data}. Error: {e}")
                if os.getenv("SENTRY_DSN"):
                    import sentry_sdk

                    sentry_sdk.capture_exception(e)
                await msg.ack()
            except Exception as e:
                logger.error(f"Error processing task: {e}", exc_info=True)
                if os.getenv("SENTRY_DSN"):
                    import sentry_sdk

                    sentry_sdk.capture_exception(e)

        if not self.subs:
            # Every subscription failed. Without this guard the gather() below
            # returns immediately, main() completes, and the container exits 0
            # into a restart loop with no obvious cause (this is exactly how
            # the workqueue filter-overlap bug presented).
            raise RuntimeError(
                "NATS worker subscribed to no subjects "
                f"({subjects_to_subscribe}); refusing to run idle. "
                "On a workqueue stream, consumer filters must not overlap: "
                "a pool that excludes tasks must not use the wildcard."
            )

        self._pull_tasks = []
        self._run_slots = asyncio.Semaphore(self.handler_concurrency())
        logger.info(
            "Worker '%s' handler concurrency cap is %s",
            self.connection_name,
            self.handler_concurrency(),
        )
        for _, sub in self.subs:
            task = asyncio.create_task(
                self._process_pull_messages(sub, message_handler)
            )
            self._pull_tasks.append(task)

        if self.handles_flow_orchestration:
            self._reclaim_task = asyncio.create_task(self._stale_claim_reaper_loop())

        await asyncio.gather(*self._pull_tasks)

    async def _process_pull_messages(self, sub, handler):
        """Fetch JetStream messages and run their handlers.

        Flow-execution workers take a slot from ``_run_slots`` (default 10)
        after a message arrives and do not wait for the hosted-agent monitor
        to finish before fetching the next one. Idle fetches do not consume
        a slot, so the cap is ten running monitors, not eight plus two waits.
        Other pools keep concurrency 1 and await each handler so drain stays
        one-in-flight.
        """
        slots = self._run_slots or asyncio.Semaphore(self.handler_concurrency())
        serial = self.handler_concurrency() == 1
        while self._accepting:
            try:
                msgs = await sub.fetch(batch=1, timeout=60)
            except nats.errors.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._accepting:
                    return
                logger.error(
                    f"Error fetching/processing messages from subscription '{sub.subject}': {e}",
                    exc_info=True,
                )
                await asyncio.sleep(1)
                continue

            for msg in msgs:
                if not self._accepting:
                    try:
                        await msg.nak()
                    except Exception:  # noqa: BLE001 - shutdown nak is best-effort
                        logger.debug(
                            "Failed to nak message during fetch shutdown",
                            exc_info=True,
                        )
                    return
                await slots.acquire()
                if not self._accepting:
                    slots.release()
                    try:
                        await msg.nak()
                    except Exception:  # noqa: BLE001 - shutdown nak is best-effort
                        logger.debug(
                            "Failed to nak message during fetch shutdown",
                            exc_info=True,
                        )
                    return
                task = asyncio.create_task(handler(msg))
                self._inflight.add(task)

                def _done(
                    t: asyncio.Task,
                    *,
                    _tasks: Set[asyncio.Task] = self._inflight,
                    _slots: asyncio.Semaphore = slots,
                ) -> None:
                    _tasks.discard(t)
                    _slots.release()

                task.add_done_callback(_done)
                if serial:
                    try:
                        await task
                    except asyncio.CancelledError:
                        raise

    async def _flow_pool_has_capacity(self) -> Optional[bool]:
        """Can the flow-execution pool take another execution right now?

        Answered from JetStream, not from this process. One worker runs the
        reaper pass for the whole pool now that the pass is leased, and its
        own semaphore says nothing about its peers. Flow consumers with
        messages still waiting to be delivered mean every replica is busy:
        what was published last pass has not been picked up, so publishing
        more of it is load with no progress behind it.

        Returns:
            False when flow tasks are already queued undelivered, True when
            the queue is empty, None when JetStream could not be asked (the
            reaper then runs its pass as before).
        """
        flow_subjects = {
            f"preloop.sync.tasks.{task}" for task in FLOW_ORCHESTRATION_TASKS
        }
        flow_subs = [
            sub
            for subject, sub in self.subs
            if subject in flow_subjects or subject == "preloop.sync.tasks.*"
        ]
        if not flow_subs:
            return None

        waiting = 0
        answered = False
        for sub in flow_subs:
            try:
                info = await sub.consumer_info()
            except Exception:  # noqa: BLE001 - an unknown answer is not a failure
                logger.debug(
                    "Could not read consumer info for a flow subscription",
                    exc_info=True,
                )
                continue
            answered = True
            waiting += int(getattr(info, "num_pending", 0) or 0)

        if not answered:
            return None
        if waiting:
            logger.debug(
                "Flow-execution pool has %s undelivered task(s) queued: no free slot",
                waiting,
            )
        return waiting == 0

    async def _stale_claim_reaper_loop(self) -> None:
        """Periodically re-dispatch stale/unclaimed active flow executions."""
        from preloop.config import settings
        from preloop.services.flow_execution_dispatcher import (
            flow_execution_worker_enabled,
        )
        from preloop.services.execution_recovery import get_recovery_service
        from preloop.models.db.session import get_db_session

        interval = max(
            5,
            int(getattr(settings, "flow_execution_reclaim_interval_seconds", 30) or 30),
        )
        logger.info(
            "Flow-execution stale-claim reaper started (interval=%ss)", interval
        )
        while self._accepting:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            if not self._accepting:
                break
            if not flow_execution_worker_enabled():
                continue
            try:
                recovery_service = get_recovery_service()
                db = next(get_db_session())
                try:
                    # The pass is leased, so only one replica per interval
                    # gets past this call; the rest return 0 immediately.
                    # Counts are logged by the pass itself, in one line.
                    await recovery_service.recover_orphaned_executions(
                        db,
                        capacity_probe=self._flow_pool_has_capacity,
                    )
                finally:
                    db.close()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Stale-claim reaper failed: %s", exc, exc_info=True)

    async def begin_drain(self) -> None:
        """Stop accepting work and hand off owned flow claims to peers."""
        if not self._accepting:
            return
        logger.info("Worker drain started: stopping new JetStream fetches")
        self._accepting = False

        if self._reclaim_task and not self._reclaim_task.done():
            self._reclaim_task.cancel()
            try:
                await self._reclaim_task
            except asyncio.CancelledError:
                # Expected: reclaim loop was cancelled as part of drain.
                logger.debug("Stale-claim reaper cancelled during drain")

        # Cancel in-flight handlers so claim_and_run finally releases+redispatches.
        inflight = list(self._inflight)
        for task in inflight:
            if not task.done():
                task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

        for pull_task in self._pull_tasks:
            if not pull_task.done():
                pull_task.cancel()
        if self._pull_tasks:
            await asyncio.gather(*self._pull_tasks, return_exceptions=True)

        if self.handles_flow_orchestration:
            try:
                from preloop.services.flow_execution_dispatcher import (
                    flow_execution_worker_enabled,
                )
                from preloop.services.flow_execution_runner import evacuate_owned_claims

                if flow_execution_worker_enabled():
                    evacuated = await evacuate_owned_claims()
                    logger.info(
                        "Drain evacuated %s remaining owned claim(s)", evacuated
                    )
            except Exception as exc:
                logger.error("Drain evacuate failed: %s", exc, exc_info=True)

    async def stop(self):
        logger.info("Worker stop signal received.")
        if getattr(self, "_price_refresher", None) is not None:
            await self._price_refresher.stop()
            self._price_refresher = None
        if getattr(self, "_price_map_refresher", None) is not None:
            await self._price_map_refresher.stop()
            self._price_map_refresher = None
        await self.begin_drain()
        for subject, sub in self.subs:
            try:
                await sub.unsubscribe()
                logger.info(f"Unsubscribed from '{subject}'.")
            except Exception as e:
                logger.error(f"Error unsubscribing from '{subject}': {e}")

        if self.nc and not self.nc.is_closed:
            logger.info("Closing worker NATS client connection...")
            try:
                await self.nc.close()
                logger.info("Worker NATS client connection closed.")
            except Exception as e:
                logger.error(f"Error closing worker NATS client connection: {e}")
        self.nc = None


async def _run_boot_recovery(tasks_allowlist: Optional[List[str]]) -> None:
    """Re-dispatch stale/unclaimed executions when this pool owns flow tasks."""
    flow_tasks = FLOW_ORCHESTRATION_TASKS
    allowlist_set = set(tasks_allowlist or [])
    if allowlist_set and not (allowlist_set & flow_tasks):
        return
    try:
        from preloop.services.flow_execution_dispatcher import (
            flow_execution_worker_enabled,
        )
        from preloop.services.execution_recovery import get_recovery_service
        from preloop.models.db.session import get_db_session

        if not flow_execution_worker_enabled():
            return
        recovery_service = get_recovery_service()
        db = next(get_db_session())
        try:
            recovered = await recovery_service.recover_orphaned_executions(db)
            logger.info(
                "Flow-execution worker boot recovery dispatched %s execution(s)",
                recovered,
            )
        finally:
            db.close()
    except Exception as recovery_error:
        logger.error(
            "Flow-execution worker recovery failed: %s",
            recovery_error,
            exc_info=True,
        )


async def main(
    tasks_allowlist: Optional[List[str]] = None,
    tasks_excludelist: Optional[List[str]] = None,
):
    # Get NATS_URL directly from environment variables
    nats_server_url = os.getenv("NATS_URL", "nats://localhost:4222")

    # Initialize the global event_bus_service for publishing flow execution updates
    # This allows FlowOrchestrator to publish real-time updates to browsers
    from preloop.sync.services.event_bus import event_bus_service

    logger.info("Initializing event bus service for flow execution updates...")
    try:
        await event_bus_service.connect()
        logger.info("Event bus service connected successfully")
    except Exception as e:
        logger.error(f"Failed to connect event bus service: {e}", exc_info=True)
        logger.warning(
            "Flow execution updates will not be published to NATS, but worker will continue"
        )

    await _run_boot_recovery(tasks_allowlist)

    queue = "preloop_sync_worker_queue"

    worker = PreloopSyncNatsWorker(
        nats_url=nats_server_url,
        queue_name=queue,
        tasks_allowlist=tasks_allowlist,
        tasks_excludelist=tasks_excludelist,
    )

    loop = asyncio.get_running_loop()
    drain_requested = asyncio.Event()

    def _request_drain() -> None:
        if not drain_requested.is_set():
            logger.info("Received shutdown signal; beginning worker drain")
            drain_requested.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_drain)
        except NotImplementedError:
            # Windows / restricted environments
            signal.signal(sig, lambda *_: _request_drain())

    listen_task = asyncio.create_task(worker.start_listening())
    drain_wait = asyncio.create_task(drain_requested.wait())

    try:
        await asyncio.wait(
            {listen_task, drain_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if drain_requested.is_set():
            await worker.begin_drain()
            if not listen_task.done():
                listen_task.cancel()
                try:
                    await listen_task
                except asyncio.CancelledError:
                    # Expected after begin_drain cancels the listen loop.
                    logger.debug("Listen task cancelled during drain")
        else:
            # listen_task finished on its own
            await listen_task
    except asyncio.CancelledError:
        logger.info("Worker task cancelled.")
    except ErrNoServers:
        logger.error(
            f"NATS Worker could not connect to {nats_server_url}. Ensure NATS is running and accessible."
        )
    except Exception as e:
        logger.error(f"NATS Worker encountered an unhandled error: {e}", exc_info=True)
    finally:
        drain_wait.cancel()
        logger.info("NATS Worker shutting down...")
        await worker.stop()
        # Close the event bus service
        await event_bus_service.close()
        logger.info("NATS Worker shutdown complete.")


if __name__ == "__main__":
    # Basic logging setup for standalone execution
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("NATS Worker interrupted by user (Ctrl+C). Exiting.")
