import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

from fastapi import WebSocket
from nats.aio.client import Client
from nats.aio.msg import Msg
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from preloop.sync.services.event_bus import get_task_publisher
from preloop.models.db.session import get_db_session as get_db
from preloop.services.account_realtime import ACCOUNT_REALTIME_TOPICS

from preloop.sync.tasks import notify_admins

logger = logging.getLogger(__name__)

# Dictionary to hold loop-specific queues to prevent test runner cross-loop panics
_log_queues: dict[asyncio.AbstractEventLoop, asyncio.Queue] = {}

# Background log persistence must never starve request-serving connections.
# Only this many threads may hold a pooled DB connection for log writes at a
# time, so a burst of NATS logs cannot consume the whole QueuePool.
LOG_PERSIST_MAX_CONCURRENCY = 1
_log_persist_semaphore = threading.BoundedSemaphore(LOG_PERSIST_MAX_CONCURRENCY)

# Bounded retry for transient pool/connection failures before dropping data.
LOG_PERSIST_MAX_ATTEMPTS = 3
LOG_PERSIST_BASE_BACKOFF_SECONDS = 0.5

# Transient errors worth retrying: pool checkout timeouts (QueuePool limit
# reached) and dropped/failed connections. Anything else is a real bug and is
# not retried.
_RETRYABLE_DB_ERRORS = (SQLAlchemyTimeoutError, OperationalError)


def get_log_queue() -> asyncio.Queue:
    """Returns the logging queue associated with the current running event loop."""
    loop = asyncio.get_running_loop()
    if loop not in _log_queues:
        _log_queues[loop] = asyncio.Queue()
    return _log_queues[loop]


def _write_log_batch(batch: List[Tuple[str, dict]]) -> None:
    """Write one batch of log records in a single transaction.

    Raises whatever the database layer raises; retry policy lives in the
    caller. DB access stays behind ``preloop.models.crud``.
    """
    from preloop.models.crud import crud_flow_execution

    db = next(get_db())
    try:
        for execution_id, log_data in batch:
            crud_flow_execution.append_log(
                db, execution_id=execution_id, log_data=log_data, commit=False
            )
        db.commit()
    except Exception:
        # Never leave a half-applied transaction on a pooled connection.
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback failure is best-effort
            logger.warning("Rollback failed while aborting log batch", exc_info=True)
        raise
    finally:
        db.close()


def _sync_batch_insert_logs(batch: list) -> bool:
    """Insert a batch of log records, retrying transient database failures.

    Pool exhaustion (``QueuePool limit ... reached``) and dropped connections
    are transient: the previous implementation dropped the whole batch on the
    first error, silently losing execution logs during load spikes. We now
    retry with exponential backoff and only drop as a last resort.

    A semaphore bounds how many threads may hold a pooled connection for log
    persistence, so background writes cannot exhaust the pool that serves user
    requests and health checks.

    Args:
        batch: Sequence of ``(execution_id, log_data)`` pairs.

    Returns:
        True if the batch was persisted, False if it was dropped.
    """
    if not batch:
        return True

    last_error: Optional[BaseException] = None

    with _log_persist_semaphore:
        for attempt in range(1, LOG_PERSIST_MAX_ATTEMPTS + 1):
            try:
                _write_log_batch(batch)
                if attempt > 1:
                    logger.info(
                        "Persisted batch of %d logs on attempt %d/%d",
                        len(batch),
                        attempt,
                        LOG_PERSIST_MAX_ATTEMPTS,
                    )
                return True
            except _RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                if attempt == LOG_PERSIST_MAX_ATTEMPTS:
                    break
                backoff = LOG_PERSIST_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Transient DB error persisting %d logs "
                    "(attempt %d/%d), retrying in %.1fs: %s",
                    len(batch),
                    attempt,
                    LOG_PERSIST_MAX_ATTEMPTS,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
            except Exception as exc:  # non-retryable: fail fast
                last_error = exc
                logger.error(
                    "Non-retryable error persisting batch of %d logs: %s",
                    len(batch),
                    exc,
                    exc_info=True,
                )
                break

    # Exhausted retries (or hit a non-retryable error): drop, but loudly.
    logger.error(
        "Dropping batch of %d logs after %d attempt(s): %s",
        len(batch),
        LOG_PERSIST_MAX_ATTEMPTS,
        last_error,
        exc_info=True,
    )
    try:
        notify_admins(
            subject="[Preloop Alert] NATS Log Persistence Failed",
            message=(
                f"A batch of {len(batch)} logs failed to persist to the database "
                f"after {LOG_PERSIST_MAX_ATTEMPTS} attempts and were dropped. "
                f"Error: {last_error}"
            ),
        )
    except Exception as alert_err:
        logger.error(f"Failed to send admin notification for dropped logs: {alert_err}")
    return False


async def _log_writer_worker():
    """
    Background worker that continuously drains the log queue and writes to the DB in batches.
    """
    while True:
        try:
            queue = get_log_queue()
            # Wait for at least one item
            item = await queue.get()
            batch = [item]

            # Greedily drain up to 500 items instantly
            while len(batch) < 500:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Dispatch the bulk insert to an isolated thread
            await asyncio.to_thread(_sync_batch_insert_logs, batch)

            # Mark all dequeued task objects as done
            for _ in batch:
                queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in log writer worker: {e}", exc_info=True)
            await asyncio.sleep(1)


async def persist_execution_log(execution_id: str, log_data: dict):
    """
    Asynchronously queues a log entry for persistence to execution_logs array in the database.

    Args:
        execution_id: ID of the flow execution
        log_data: Log message data to append
    """
    try:
        # Puts the item into the queue without blocking the NATS consumer event loop
        get_log_queue().put_nowait((execution_id, log_data))
    except Exception as e:
        logger.error(
            f"Failed to queue log for execution {execution_id}: {e}", exc_info=True
        )


class WebSocketManager:
    """
    Manages WebSocket connections for real-time updates with account-based filtering.
    """

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.connection_accounts: Dict[str, str] = {}  # connection_id -> account_id
        self.connection_topics: Dict[str, Set[str]] = {}  # connection_id -> topics

    async def connect(self, websocket: WebSocket) -> str:
        """
        Accepts a new WebSocket connection and returns a unique ID for it.
        For backward compatibility - no account filtering.
        """
        await websocket.accept()
        connection_id = str(uuid.uuid4())
        self.active_connections[connection_id] = websocket
        logger.info(f"New WebSocket connection {connection_id} established.")
        logger.info(f"Total active connections: {len(self.active_connections)}")
        return connection_id

    async def connect_with_account(self, websocket: WebSocket, account_id: str) -> str:
        """
        Accepts a new WebSocket connection with account ID for filtering.

        Args:
            websocket: WebSocket connection
            account_id: Account ID for filtering broadcasts

        Returns:
            connection_id: Unique identifier for this connection
        """
        connection_id = str(uuid.uuid4())
        self.active_connections[connection_id] = websocket
        self.connection_accounts[connection_id] = account_id
        logger.info(
            f"New WebSocket connection {connection_id} established for account {account_id}."
        )
        logger.info(f"Total active connections: {len(self.active_connections)}")
        return connection_id

    def subscribe(self, connection_id: str, topic: str) -> bool:
        """Subscribe one connection to a supported topic."""
        normalized_topic = self.normalize_topic(topic)
        if connection_id not in self.active_connections or normalized_topic is None:
            return False
        self.connection_topics.setdefault(connection_id, set()).add(normalized_topic)
        return True

    def unsubscribe(self, connection_id: str, topic: str) -> bool:
        """Unsubscribe one connection from a topic."""
        normalized_topic = self.normalize_topic(topic)
        if connection_id not in self.active_connections or normalized_topic is None:
            return False
        topics = self.connection_topics.get(connection_id)
        if not topics or normalized_topic not in topics:
            return False
        topics.remove(normalized_topic)
        if not topics:
            self.connection_topics.pop(connection_id, None)
        return True

    def get_subscriptions(self, connection_id: str) -> Set[str]:
        """Return the active subscriptions for one connection."""
        return set(self.connection_topics.get(connection_id, set()))

    @staticmethod
    def normalize_topic(topic: Optional[str]) -> Optional[str]:
        """Normalize and validate one subscription topic."""
        if not topic:
            return None
        normalized_topic = topic.strip()
        if normalized_topic in ACCOUNT_REALTIME_TOPICS:
            return normalized_topic
        return None

    @classmethod
    def resolve_topic(cls, data: dict) -> Optional[str]:
        """Resolve a routing topic from one outgoing message."""
        explicit_topic = cls.normalize_topic(data.get("topic"))
        if explicit_topic:
            return explicit_topic

        message_type = data.get("type")
        if not message_type:
            return None
        if message_type.startswith("approval_"):
            return "approvals"
        if message_type == "activity_update":
            return "activity"
        if message_type in {
            "execution_started",
            "status_update",
            "agent_log_line",
            "execution_completed",
            "execution_failed",
            "model_gateway_call",
            "tool_call",
            "mcp_call",
            "tool_calls_update",
            "token_usage_update",
            "budget_update",
            "model_output",
            "agent_started",
            "agent_stopped",
            "connected",
        }:
            return "flow_executions"
        return None

    def _accepts_topic(self, connection_id: str, topic: Optional[str]) -> bool:
        topics = self.connection_topics.get(connection_id)
        if not topics or topic is None:
            return True
        return topic in topics

    def disconnect(self, connection_id: str):
        """
        Disconnects a WebSocket and removes account association.
        """
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
            logger.info(f"WebSocket connection {connection_id} closed.")

        if connection_id in self.connection_accounts:
            del self.connection_accounts[connection_id]
        self.connection_topics.pop(connection_id, None)

        logger.info(f"Total active connections: {len(self.active_connections)}")

    async def broadcast(self, message: str, account_id: str = None):
        """
        Broadcasts a message to connected clients, optionally filtered by account_id.

        Args:
            message: Message to broadcast
            account_id: If provided, only send to connections with matching account_id
        """
        sent_count = 0
        for connection_id, connection in list(self.active_connections.items()):
            # If account_id is specified, only send to connections with matching account
            if account_id is not None:
                conn_account = self.connection_accounts.get(connection_id)
                if conn_account != account_id:
                    continue

            try:
                await connection.send_text(message)
                sent_count += 1
                logger.debug(f"Sent message to connection {connection_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to send message to connection {connection_id}: {e}"
                )

        # Only log broadcast completion at debug level to avoid log spam
        if account_id and sent_count > 0:
            logger.debug(
                f"Broadcast complete: sent to {sent_count} connection(s) for account {account_id}"
            )

    def _count_account_connections(self, account_id: str) -> int:
        """Count connections currently bound to one account.

        Args:
            account_id: Account whose listeners should be counted.

        Returns:
            Number of active connections registered to ``account_id``.
        """
        return sum(1 for acc in self.connection_accounts.values() if acc == account_id)

    async def broadcast_json(self, data: dict, account_id: str = None):
        """
        Broadcasts a JSON message to connected clients, optionally filtered by account_id.

        Args:
            data: Data to broadcast as JSON
            account_id: If provided, only send to connections with matching account_id
        """
        # Skip logging for high-frequency message types when no one is listening
        # to avoid log spam that can crash the pod
        msg_type = data.get("type", "unknown")
        high_freq_types = {"agent_log_line", "token_usage_update", "tool_calls_update"}

        if account_id:
            # The matching count only ever feeds a log line, so it is computed
            # lazily. Counting every connection on each broadcast was pure
            # overhead on the hottest path (agent_log_line), where the result is
            # discarded whenever DEBUG is off.
            if msg_type in high_freq_types:
                # High-frequency messages only ever log at DEBUG, so skip the
                # scan entirely when that line would be discarded anyway.
                if logger.isEnabledFor(logging.DEBUG):
                    matching_count = self._count_account_connections(account_id)
                    if matching_count > 0:
                        logger.debug(
                            f"Broadcasting {msg_type} to {matching_count} "
                            f"connection(s) for account {account_id}"
                        )
            else:
                # Low-volume messages: one scan feeds either the INFO line (when
                # someone is listening) or the DEBUG "no listeners" line, which
                # was ~69% of all gateway log volume in production.
                matching_count = self._count_account_connections(account_id)
                if matching_count > 0:
                    logger.info(
                        f"Broadcasting {msg_type} to account_id={account_id}, "
                        f"matching_connections={matching_count}"
                    )
                else:
                    logger.debug(
                        f"Broadcasting {msg_type} to account_id={account_id}, "
                        f"matching_connections=0"
                    )
        else:
            logger.info(
                f"Broadcasting {msg_type} to all {len(self.active_connections)} connections"
            )

        topic = self.resolve_topic(data)
        sent_count = 0
        encoded = json.dumps(data)
        for connection_id, connection in list(self.active_connections.items()):
            if account_id is not None:
                conn_account = self.connection_accounts.get(connection_id)
                if conn_account != account_id:
                    continue
            if not self._accepts_topic(connection_id, topic):
                continue
            try:
                await connection.send_text(encoded)
                sent_count += 1
            except Exception as e:
                logger.warning(
                    f"Failed to send message to connection {connection_id}: {e}"
                )

        if account_id and sent_count > 0:
            logger.debug(
                "Broadcast complete: sent %s message(s) for account %s topic=%s",
                sent_count,
                account_id,
                topic,
            )


async def nats_consumer(manager: "WebSocketManager"):
    """
    Consumes messages from NATS and broadcasts them to WebSocket clients.
    Includes account-based filtering for security - only broadcasts to clients
    with matching account_id.
    Also persists execution logs to the database.
    """
    task_publisher = await get_task_publisher()
    nats_client: Client = task_publisher.nc
    if not nats_client or not nats_client.is_connected:
        logger.error("NATS client not available or not connected.")
        return

    async def message_handler(msg: Msg):
        try:
            data = json.loads(msg.data.decode())

            # Extract account_id for filtering
            account_id = data.get("account_id")

            # Broadcast to WebSocket clients with account filtering
            # Only clients with matching account_id will receive the message
            if account_id:
                await manager.broadcast_json(data, account_id=str(account_id))
            else:
                # If no account_id in message, log warning but still broadcast
                # (for backward compatibility during migration)
                logger.warning(
                    f"Flow update message missing account_id: {data.get('type')} "
                    f"for execution {data.get('execution_id')}"
                )
                await manager.broadcast_json(data)

        except json.JSONDecodeError:
            error_msg = f"Received non-JSON message from NATS: {msg.data.decode()}"
            logger.warning(error_msg)
            try:
                asyncio.create_task(
                    asyncio.to_thread(
                        notify_admins,
                        subject="[Preloop Alert] Malformed NATS Message Dropped",
                        message=error_msg,
                    )
                )
            except Exception:
                # Best-effort admin alert; malformed JSON handling continues below.
                pass
        except Exception as e:
            logger.error(f"Error processing NATS message: {e}")
            try:
                asyncio.create_task(
                    asyncio.to_thread(
                        notify_admins,
                        subject="[Preloop Alert] NATS Message Processing Failed",
                        message=f"An exception occurred while processing a NATS message: {str(e)}",
                    )
                )
            except Exception:
                # Best-effort admin alert; log the original processing error above.
                pass

    async def persistence_handler(msg: Msg):
        try:
            data = json.loads(msg.data.decode())
            execution_id = data.get("execution_id")
            if execution_id:
                await persist_execution_log(execution_id, data)
        except Exception as e:
            logger.error(f"Error persisting log: {e}")

    try:
        # Subscribe to a wildcard subject to receive all flow updates
        await nats_client.subscribe("flow-updates.*", cb=message_handler)
        logger.info("Subscribed to NATS subject 'flow-updates.*'")

        # Persist logs with a queue group so only one instance writes them to DB
        await nats_client.subscribe(
            "flow-updates.*", queue="log-persisters", cb=persistence_handler
        )
        logger.info(
            "Subscribed to NATS subject 'flow-updates.*' with queue group 'log-persisters'"
        )

        await nats_client.subscribe("account-updates.*", cb=message_handler)
        logger.info("Subscribed to NATS subject 'account-updates.*'")

        # Subscribe to approval updates
        await nats_client.subscribe("approval-updates", cb=message_handler)
        logger.info("Subscribed to NATS subject 'approval-updates'")

        # Subscribe to admin activity updates (for admin dashboard)
        await nats_client.subscribe("admin.activity", cb=message_handler)
        logger.info("Subscribed to NATS subject 'admin.activity'")

        # Start the background log writer worker task
        log_worker_task = asyncio.create_task(_log_writer_worker())

        try:
            # Keep the consumer running
            while True:
                await asyncio.sleep(1)
        finally:
            log_worker_task.cancel()
            try:
                await log_worker_task
            except asyncio.CancelledError:
                # Background log writer was cancelled during consumer shutdown.
                pass

    except Exception as e:
        logger.error(f"NATS consumer failed: {e}")


# Create a single instance of the manager to be used across the application
manager = WebSocketManager()
