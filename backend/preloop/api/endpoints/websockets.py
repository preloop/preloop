import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_user_from_token_if_valid_sync
from preloop.services.db_executor import detach_user, run_db_async
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models import User
from preloop.services.activity_tracker import handle_activity
from preloop.services.session_manager import session_manager
from preloop.services.websocket_manager import manager
from preloop.sync.services.event_bus import EventBus, get_nats_client
from preloop.utils import get_client_ip

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_token_user(token: str) -> Optional[User]:
    """Validate a token using a short-lived database session."""

    def _lookup(db: Session) -> Optional[User]:
        user = get_user_from_token_if_valid_sync(token, db)
        return detach_user(db, user)

    return await run_db_async(_lookup)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for streaming Flow execution updates.
    Includes authentication and account-based filtering.
    Includes a heartbeat to keep the connection alive.

    Only broadcasts flow execution updates that belong to the authenticated user's account.

    Authentication is handled by WebSocketAuthMiddleware which validates the
    Bearer token from the Authorization header during HTTP upgrade.
    """
    await websocket.accept()

    # Get authenticated user from middleware (set during HTTP upgrade)
    user = websocket.scope.get("state", {}).get("user")
    if not user:
        # Middleware already rejected unauthenticated requests to /ws,
        # but handle edge case for safety
        logger.warning("WebSocket /ws: no authenticated user in scope")
        await websocket.send_json({"error": "Authentication required"})
        await websocket.close(code=1008)
        return

    # Get connection info
    client_ip = get_client_ip(websocket)
    user_agent = websocket.headers.get("user-agent", "")

    # Detect mobile client from user-agent
    is_mobile = any(
        x in user_agent.lower()
        for x in ["preloopai", "iphone", "ipad", "android", "mobile"]
    )
    client_type = "mobile_app" if is_mobile else "browser"

    logger.info(
        f"WebSocket authenticated for user {user.username} (account {user.account_id}) "
        f"from {client_ip} via {client_type}"
    )

    # Create session for tracking in admin UI
    session = await session_manager.create_session(
        websocket=websocket,
        user=user,
        fingerprint=None,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    # Add client type metadata
    session.metadata["client_type"] = client_type
    session.metadata["endpoint"] = "/ws"

    # Connect with account_id for filtering
    connection_id = await manager.connect_with_account(websocket, str(user.account_id))

    logger.info(
        f"WebSocket session {session.id} established for {user.username} "
        f"(connection_id={connection_id}, type={client_type})"
    )

    try:
        while True:
            # Wait for a message from the client (e.g., a pong response)
            # Set a timeout to detect unresponsive clients.
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)

                # Update session activity
                session_manager.update_activity(session.id)

                if message == "pong":
                    # Client is alive
                    continue

                # Log user interaction events
                try:
                    event_data = json.loads(message)
                    logger.debug(
                        f"Received event from {connection_id} ({client_type}): {event_data.get('type', 'unknown')}"
                    )
                except json.JSONDecodeError:
                    logger.debug(
                        f"Received non-JSON message from {connection_id}: {message[:50]}"
                    )

            except asyncio.TimeoutError:
                # No message received in time, send a ping.
                await websocket.send_text("ping")
                # Wait for a pong response
                try:
                    response = await asyncio.wait_for(
                        websocket.receive_text(), timeout=10.0
                    )
                    if response != "pong":
                        logger.warning(
                            f"Session {session.id} expected pong, got: {response[:50]}"
                        )
                        break
                except asyncio.TimeoutError:
                    logger.info(
                        f"Session {session.id} did not respond to ping, disconnecting"
                    )
                    break  # Client did not respond to ping, assume disconnected.
    except WebSocketDisconnect:
        logger.info(f"Session {session.id} disconnected normally")
    except Exception as e:
        logger.error(f"Error in WebSocket session {session.id}: {e}", exc_info=True)
    finally:
        manager.disconnect(connection_id)
        try:
            await session_manager.end_session(session.id)
        except Exception as e:
            logger.error(f"Error ending session {session.id}: {e}")


@router.websocket("/ws/flow-executions/{execution_id}")
async def flow_execution_websocket(
    websocket: WebSocket,
    execution_id: str,
):
    """
    WebSocket endpoint for bidirectional flow execution monitoring.

    Streams:
    - Real-time logs from agent execution (line-by-line)
    - Status updates (every 5 seconds)
    - Parsed actions and MCP calls

    Accepts commands:
    - {"command": "stop"} - Stop execution
    - {"command": "send_message", "message": "..."} - Send message to agent
    - {"command": "pause"} - Pause execution (future)

    Args:
        websocket: WebSocket connection
        execution_id: UUID of the flow execution to monitor
    """
    await websocket.accept()

    user = websocket.scope.get("state", {}).get("user")

    if not user:
        token = websocket.query_params.get("token")
        if token:
            user = await _resolve_token_user(token)
            if not user:
                logger.warning(
                    f"Invalid token for WebSocket connection to execution {execution_id}"
                )
                await websocket.send_json(
                    {"error": "Invalid or expired authentication token"}
                )
                await websocket.close(code=1008)
                return
        else:
            logger.warning(
                f"WebSocket connection attempted without authentication for execution {execution_id}"
            )
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=1008)
            return

    def _load_execution(db: Session):
        execution = crud_flow_execution.get(db, id=execution_id)
        if not execution:
            return None
        flow = crud_flow.get(db, id=execution.flow_id)
        if not flow:
            return "missing_flow"
        return flow.account_id

    auth_result = await run_db_async(_load_execution)
    if auth_result is None:
        await websocket.send_json({"error": "Execution not found"})
        await websocket.close(code=1008)
        return

    if auth_result == "missing_flow":
        logger.error(f"Flow not found for execution {execution_id}")
        await websocket.send_json({"error": "Flow not found"})
        await websocket.close(code=1008)
        return

    flow_account_id = auth_result
    if flow_account_id and flow_account_id != user.account_id:
        logger.warning(
            f"User {user.username} (account {user.account_id}) attempted to access flow "
            f"(account {flow_account_id}) via WebSocket for execution {execution_id}"
        )
        await websocket.send_json(
            {"error": "Unauthorized - you do not have access to this flow execution"}
        )
        await websocket.close(code=1008)
        return

    logger.info(
        f"WebSocket authorized for user {user.username} to access execution {execution_id}"
    )

    # Connect to NATS event bus
    event_bus = EventBus()
    nats_sub = None

    try:
        await event_bus.connect()
        logger.info(f"WebSocket connected for execution {execution_id}")

        # Subscribe to NATS updates for this execution
        update_subject = f"flow-updates.{execution_id}"

        async def nats_message_handler(msg):
            """Forward NATS messages to WebSocket client."""
            try:
                data = json.loads(msg.data.decode())
                await websocket.send_json(data)
            except Exception as e:
                logger.error(f"Error forwarding NATS message to WebSocket: {e}")

        nats_sub = await event_bus.nc.subscribe(update_subject, cb=nats_message_handler)
        logger.info(f"Subscribed to NATS subject: {update_subject}")

        # Send initial connection confirmation
        await websocket.send_json(
            {
                "type": "connected",
                "execution_id": execution_id,
                "message": "Connected to flow execution stream",
            }
        )

        # Listen for commands from WebSocket client
        while True:
            try:
                data = await websocket.receive_json()

                # Validate command structure
                if "command" not in data:
                    await websocket.send_json({"error": "Missing 'command' field"})
                    continue

                command = data["command"]
                logger.info(
                    f"Received command '{command}' for execution {execution_id}"
                )

                # Publish command to NATS for orchestrator to handle
                command_subject = f"flow-commands.{execution_id}"
                await event_bus.nc.publish(command_subject, json.dumps(data).encode())

                # Acknowledge command
                await websocket.send_json(
                    {
                        "type": "command_ack",
                        "command": command,
                        "message": f"Command '{command}' sent",
                    }
                )

            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for execution {execution_id}")
                break
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON"})
            except Exception as e:
                logger.error(f"Error processing WebSocket message: {e}", exc_info=True)
                await websocket.send_json({"error": f"Error processing message: {e}"})

    except Exception as e:
        logger.error(
            f"Error in WebSocket connection for execution {execution_id}: {e}",
            exc_info=True,
        )
        await websocket.send_json({"error": f"Connection error: {e}"})

    finally:
        # Cleanup
        if nats_sub:
            try:
                await nats_sub.unsubscribe()
            except Exception as e:
                logger.error(f"Error unsubscribing from NATS: {e}")

        try:
            await event_bus.close()
        except Exception as e:
            logger.error(f"Error closing NATS connection: {e}")

        try:
            await websocket.close()
        except Exception:
            pass  # Already closed

        logger.info(f"WebSocket connection closed for execution {execution_id}")


@router.websocket("/ws/unified")
async def unified_websocket(websocket: WebSocket):
    """Unified WebSocket endpoint for all real-time updates.

    Features:
    - Single persistent connection per user
    - Supports authenticated and anonymous users
    - Activity tracking and analytics
    - Message routing to appropriate handlers
    - Automatic session management

    Authentication:
        - Bearer token in Authorization header (preferred)
        - Token query param (backwards compatibility)
        - Anonymous if no token provided

    Query Parameters:
        fingerprint (optional): Browser fingerprint for anonymous users
    """
    await websocket.accept()

    # Get authenticated user from middleware (set during HTTP upgrade)
    # Middleware validates Bearer token from Authorization header
    user = websocket.scope.get("state", {}).get("user")

    # Extract fingerprint for anonymous tracking
    fingerprint = websocket.query_params.get("fingerprint")

    # Get real IP address (behind ingress)
    client_ip = get_client_ip(websocket)
    user_agent = websocket.headers.get("user-agent", "")

    # Create session
    session = await session_manager.create_session(
        websocket=websocket,
        user=user,
        fingerprint=fingerprint,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    logger.info(
        f"Unified WebSocket session {session.id} established for "
        f"{session.display_name} from {client_ip}"
    )

    # Start heartbeat monitoring
    heartbeat_task = None
    manager_connection_id = None

    try:
        # Register connection with the existing WebSocket manager for broadcast compatibility
        if user:
            manager_connection_id = await manager.connect_with_account(
                websocket, str(user.account_id)
            )
        else:
            # For anonymous users, register without account filtering
            manager_connection_id = str(session.connection_id)
            manager.active_connections[manager_connection_id] = websocket

        # Send initial handshake confirmation
        await websocket.send_json(
            {
                "type": "handshake",
                "session_id": session.id,
                "authenticated": session.is_authenticated,
                "message": "Connected to unified WebSocket",
            }
        )

        # Message loop
        while True:
            try:
                # Wait for message with timeout for heartbeat
                # Use receive_text() instead of receive_json() to handle non-JSON messages
                text = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)

                # Try to parse as JSON
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    # Log non-JSON messages with client info to identify problematic clients
                    user_agent = websocket.headers.get("user-agent", "unknown")
                    logger.warning(
                        f"Received non-JSON message from session {session.id} "
                        f"(User-Agent: {user_agent}): {text[:200]} - Error: {e}"
                    )
                    continue

                # Update activity timestamp
                session_manager.update_activity(session.id)

                # Handle different message types
                message_type = data.get("type")

                if message_type == "authenticate":
                    # Message-based authentication for browsers
                    token = data.get("token")
                    if not token:
                        await websocket.send_json(
                            {
                                "type": "auth_error",
                                "error": "Token required for authentication",
                            }
                        )
                        continue

                    # Validate token
                    auth_user = await _resolve_token_user(token)
                    if not auth_user:
                        await websocket.send_json(
                            {"type": "auth_error", "error": "Invalid or expired token"}
                        )
                        continue

                    await session_manager.upgrade_session(session.id, auth_user)
                    user = auth_user

                    # Re-register with manager for account-based broadcasts
                    subscribed_topics = (
                        manager.get_subscriptions(manager_connection_id)
                        if manager_connection_id
                        else set()
                    )

                    # Remove old anonymous registration first
                    if (
                        manager_connection_id
                        and manager_connection_id in manager.active_connections
                    ):
                        del manager.active_connections[manager_connection_id]

                    # Register with account filtering for broadcast messages
                    manager_connection_id = await manager.connect_with_account(
                        websocket, str(user.account_id)
                    )
                    for topic in subscribed_topics:
                        manager.subscribe(manager_connection_id, topic)

                    await websocket.send_json(
                        {
                            "type": "authenticated",
                            "user": {
                                "id": str(user.id),
                                "username": user.username,
                                "email": user.email,
                            },
                        }
                    )
                    logger.info(
                        f"Session {session.id} authenticated via message as {user.username}"
                    )

                elif message_type == "activity":
                    await handle_activity(data, session)

                elif message_type == "command":
                    # Handle commands for flow executions (stop, send_message, etc.)
                    command = data.get("command")
                    execution_id = data.get("execution_id")
                    payload = data.get("payload")

                    logger.info(
                        f"Received command '{command}' from session {session.id} "
                        f"for execution {execution_id}"
                    )

                    if execution_id and command:
                        # Forward command to NATS for orchestrator to handle
                        try:
                            nc = await get_nats_client()
                            if nc and nc.is_connected:
                                command_subject = f"flow-commands.{execution_id}"
                                command_data = {
                                    "command": command,
                                    "payload": payload or {},
                                    "message": data.get("message"),  # For send_message
                                }
                                await nc.publish(
                                    command_subject, json.dumps(command_data).encode()
                                )
                                logger.info(f"Published command to {command_subject}")
                            else:
                                logger.warning(
                                    "NATS not connected, cannot forward command"
                                )
                        except Exception as e:
                            logger.error(f"Failed to forward command to NATS: {e}")
                    else:
                        logger.warning(
                            f"Command missing execution_id or command: {data}"
                        )

                elif message_type == "subscribe":
                    # Handle topic subscriptions
                    topic = data.get("topic")
                    if (
                        manager_connection_id is None
                        or not isinstance(topic, str)
                        or not manager.subscribe(manager_connection_id, topic)
                    ):
                        await websocket.send_json(
                            {
                                "type": "subscription_error",
                                "topic": topic,
                                "error": "Unsupported topic",
                            }
                        )
                        continue
                    logger.info(f"Session {session.id} subscribed to topic: {topic}")
                    await websocket.send_json(
                        {
                            "type": "subscription_ack",
                            "topic": topic,
                            "action": "subscribed",
                            "topics": sorted(
                                manager.get_subscriptions(manager_connection_id)
                            ),
                        }
                    )

                elif message_type == "unsubscribe":
                    # Handle topic unsubscriptions
                    topic = data.get("topic")
                    if (
                        manager_connection_id is None
                        or not isinstance(topic, str)
                        or not manager.unsubscribe(manager_connection_id, topic)
                    ):
                        await websocket.send_json(
                            {
                                "type": "subscription_error",
                                "topic": topic,
                                "error": "Subscription not found",
                            }
                        )
                        continue
                    logger.info(
                        f"Session {session.id} unsubscribed from topic: {topic}"
                    )
                    await websocket.send_json(
                        {
                            "type": "subscription_ack",
                            "topic": topic,
                            "action": "unsubscribed",
                            "topics": sorted(
                                manager.get_subscriptions(manager_connection_id)
                            ),
                        }
                    )

                elif message_type == "pong":
                    # Heartbeat response from client
                    continue

                elif message_type == "ping":
                    # Heartbeat request from client - respond with pong
                    await websocket.send_json({"type": "pong"})

                else:
                    logger.warning(
                        f"Unknown message type '{message_type}' from session {session.id}"
                    )

            except asyncio.TimeoutError:
                # Send ping for heartbeat
                try:
                    await websocket.send_json({"type": "ping"})
                    # Wait for pong response
                    pong_text = await asyncio.wait_for(
                        websocket.receive_text(), timeout=10.0
                    )
                    try:
                        pong = json.loads(pong_text)
                        if pong.get("type") != "pong":
                            logger.warning(
                                f"Expected pong from session {session.id}, got: {pong}"
                            )
                            break
                    except json.JSONDecodeError:
                        # Non-JSON response to ping, treat as invalid
                        logger.warning(
                            f"Session {session.id} sent non-JSON response to ping: {pong_text[:100]}"
                        )
                        break
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Session {session.id} did not respond to ping, disconnecting - "
                        f"User: {session.display_name}, "
                        f"IP: {client_ip}, "
                        f"User-Agent: {user_agent[:100]}"
                    )
                    break

    except WebSocketDisconnect as e:
        # Log detailed disconnection info
        # Get session once to avoid TOCTOU race condition
        current_session = session_manager.sessions.get(session.id)
        if current_session:
            duration = (
                current_session.last_activity - current_session.connected_at
            ).total_seconds()
            duration_str = f"{duration}s"
        else:
            duration_str = "unknown"
        username = session.display_name
        logger.info(
            f"Session {session.id} disconnected - "
            f"User: {username}, "
            f"Duration: {duration_str}, "
            f"Reason: {e}"
        )
    except Exception as e:
        from sqlalchemy.exc import OperationalError

        username = session.display_name
        if isinstance(e, OperationalError):
            logger.warning(
                "Unified WebSocket session %s lost database connection: %s "
                "(User: %s, IP: %s)",
                session.id,
                e,
                username,
                client_ip,
            )
        else:
            logger.error(
                f"Error in unified WebSocket session {session.id}: {e}, "
                f"User: {username}, "
                f"IP: {client_ip}",
                exc_info=True,
            )
    finally:
        # Cleanup
        if heartbeat_task:
            heartbeat_task.cancel()

        # Disconnect from manager (only if connection was established)
        if manager_connection_id is not None:
            try:
                manager.disconnect(manager_connection_id)
            except Exception as e:
                logger.error(f"Error disconnecting from manager: {e}")

        try:
            await session_manager.end_session(session.id)
        except Exception as e:
            logger.error(f"Error ending session: {e}")

        # Close WebSocket
        try:
            await websocket.close()
        except Exception:
            pass  # Already closed

        logger.info(f"Unified WebSocket session {session.id} closed")
