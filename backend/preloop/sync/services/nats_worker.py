"""
Preloop Sync NATS Worker
Subscribes to NATS messages and triggers tracker synchronization.
"""

import asyncio
import json
import logging
import os
import datetime
import inspect
import nats
import socket
import uuid
from typing import List, Optional, Tuple
from nats.aio.client import Client as NATSClient
from nats.aio.errors import ErrNoServers
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import APIError

import preloop.sync.tasks as tasks
from preloop.sync.config import logger


class PreloopSyncNatsWorker:
    def __init__(
        self,
        nats_url: str,
        queue_name: str,
        tasks_allowlist: Optional[List[str]] = None,
    ):
        self.nats_url = nats_url
        self.queue_name = queue_name
        self.tasks_allowlist = tasks_allowlist or []
        self.nc: NATSClient = None
        self.js = None
        self.subs: List[Tuple[str, nats.aio.client.Subscription]] = []
        self.connection_name = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

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

        subjects_to_subscribe = []
        if self.tasks_allowlist:
            for task_name in self.tasks_allowlist:
                subjects_to_subscribe.append(f"preloop.sync.tasks.{task_name}")
        else:
            subjects_to_subscribe.append("preloop.sync.tasks.*")

        logger.info(
            f"Worker '{self.connection_name}' subscribing to subjects: {subjects_to_subscribe}"
        )

        for subject in subjects_to_subscribe:
            try:
                # For a durable, filtered consumer, the durable name must be unique
                # for each subject filter. We construct it from the queue name
                # and the task name (the last part of the subject).
                sanitized_subject = subject.replace(".", "-").replace("*", "all")
                durable_name = f"{self.queue_name}_{sanitized_subject}"

                # 1. Explicitly create or update the consumer. This is an
                # idempotent operation that ensures the consumer exists on the
                # server with the correct configuration.
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
                    return

                if inspect.iscoroutinefunction(func):
                    stats = await func(
                        *payload.get("args", []), **payload.get("kwargs", {})
                    )
                else:
                    stats = func(*payload.get("args", []), **payload.get("kwargs", {}))
                await msg.ack()

                end_time = datetime.datetime.now()
                logger.info(
                    f"Task '{task_name}' completed and acknowledged. Stats: {stats}. Duration: {end_time - start_time}"
                )

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

        tasks_to_await = []
        for _, sub in self.subs:
            tasks_to_await.append(
                asyncio.create_task(self._process_pull_messages(sub, message_handler))
            )

        await asyncio.gather(*tasks_to_await)

    async def _process_pull_messages(self, sub, handler):
        """
        Continuously fetches and processes messages from a pull subscription.
        """
        while True:
            try:
                # Fetch a single message, waiting up to 60 seconds.
                msgs = await sub.fetch(batch=1, timeout=60)
                for msg in msgs:
                    await handler(msg)
            except nats.errors.TimeoutError:
                # This is expected when no messages are available. Continue polling.
                continue
            except Exception as e:
                logger.error(
                    f"Error fetching/processing messages from subscription '{sub.subject}': {e}",
                    exc_info=True,
                )
                # Avoid a tight loop on persistent errors.
                await asyncio.sleep(1)

    async def stop(self):
        logger.info("Worker stop signal received.")
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


async def main(tasks_allowlist: Optional[List[str]] = None):
    # Configuration should ideally come from environment variables or a config file
    # Using preloop_settings.nats_url as an example, assuming it's accessible
    # and correctly configured for Preloop Sync's environment.
    # If Preloop Sync has its own settings management (e.g. preloop_sync_settings), use that.

    # Fallback to a default NATS URL if not found in settings, or make it mandatory.
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

    queue = "preloop_sync_worker_queue"

    worker = PreloopSyncNatsWorker(
        nats_url=nats_server_url,
        queue_name=queue,
        tasks_allowlist=tasks_allowlist,
    )

    try:
        await worker.start_listening()
    except asyncio.CancelledError:
        logger.info("Worker task cancelled.")
    except ErrNoServers:
        logger.error(
            f"NATS Worker could not connect to {nats_server_url}. Ensure NATS is running and accessible."
        )
    except Exception as e:
        logger.error(f"NATS Worker encountered an unhandled error: {e}", exc_info=True)
    finally:
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
