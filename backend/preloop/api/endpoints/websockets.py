import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_user_from_token_if_valid_sync
from preloop.api.auth.key_scopes import api_key_allowed_on_channel
from preloop.services.db_executor import detach_user, run_db_async
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models import User
from preloop.services.activity_tracker import handle_activity
from preloop.services.session_manager import session_manager
from preloop.services.websocket_manager import SessionStreamFilter, manager
from preloop.sync.services.event_bus import EventBus, get_nats_client
from preloop.utils import get_client_ip
from preloop.utils.permissions import require_permission

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_token_user(token: str) -> Optional[User]:
    """Validate a token using a short-lived database session."""

    def _lookup(db: Session) -> Optional[User]:
        user = get_user_from_token_if_valid_sync(token, db)
        if user is not None and not api_key_allowed_on_channel(
            getattr(user, "_auth_api_key", None), "console websocket"
        ):
            return None
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
    execution_id: uuid.UUID,
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
                logger.warning("Invalid token for WebSocket connection")
                await websocket.send_json(
                    {"error": "Invalid or expired authentication token"}
                )
                await websocket.close(code=1008)
                return
        else:
            logger.warning("WebSocket connection attempted without authentication")
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=1008)
            return

    def _load_execution(db: Session):
        execution = crud_flow_execution.get(db, id=str(execution_id))
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
                "execution_id": str(execution_id),
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

        # Send initial handshake confirmation. The client may already have
        # disconnected between accept() and this send — uvloop then raises a
        # bare RuntimeError ("unable to perform operation on <TCPTransport
        # closed=True ...>"). Treat that as a normal early disconnect rather
        # than an unexpected server error.
        try:
            await websocket.send_json(
                {
                    "type": "handshake",
                    "session_id": session.id,
                    "authenticated": session.is_authenticated,
                    "message": "Connected to unified WebSocket",
                }
            )
        except RuntimeError as e:
            logger.info(
                f"Session {session.id} disconnected before handshake completed: {e}"
            )
            return

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


# --- Session attach (#1149) -------------------------------------------------
#
# ``preloop sessions attach`` follows one session from a terminal. The unified
# socket fans out every event in the account and leaves filtering to the
# browser, which is fine for the console but would hand a terminal the whole
# account's traffic and skip the session read check. This channel is the
# session-scoped one the issue allows for: it authenticates the bearer token,
# requires session read on a session in the caller's account, withholds
# approval payloads from a viewer who cannot read approvals, audits attach and
# detach, and forwards only the events that belong to the session.
#
# It is receive-only. Notes and approval decisions go through their REST
# endpoints, so they keep their own permission checks and audit. A command
# channel for Agent Control agents (#1150) would be a new message type here.

SESSION_ATTACH_AUDIT_ATTACHED = "runtime_session.attached"
SESSION_ATTACH_AUDIT_DETACHED = "runtime_session.detached"

#: Close codes in the private 4000 range, mirroring the HTTP status meant.
SESSION_ATTACH_CLOSE_UNAUTHORIZED = 4401
SESSION_ATTACH_CLOSE_FORBIDDEN = 4403
SESSION_ATTACH_CLOSE_NOT_FOUND = 4404


@require_permission("view_runtime_sessions")
def _session_read_allowed(*, current_user: User, db: Session) -> bool:
    """Apply the same RBAC check the session REST reads use."""
    return True


@require_permission("view_approvals")
def _approval_read_allowed(*, current_user: User, db: Session) -> bool:
    """Apply the same RBAC check the approval REST reads use."""
    return True


def _bearer_token(websocket: WebSocket) -> Optional[str]:
    """Read the bearer token from the upgrade request's Authorization header.

    Header only: a token in the query string ends up in every access log on
    the way, and the CLI can always set a header.
    """
    header = websocket.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        token = header[7:].strip()
        return token or None
    return None


def _authorize_session_attach(
    db: Session,
    *,
    user: User,
    runtime_session_id: str,
    execution_id: Optional[str],
) -> dict:
    """Decide whether ``user`` may follow the session, without side effects.

    Returns a dict with ``error`` (an HTTP-like status) on refusal, otherwise
    the session's identity and whether approvals may be shown.
    """
    from preloop.models.crud import crud_runtime_session

    try:
        _session_read_allowed(current_user=user, db=db)
    except HTTPException as exc:
        if exc.status_code == 403:
            return {"error": 403, "detail": "Attaching needs session read access"}
        raise
    try:
        uuid.UUID(runtime_session_id)
    except ValueError:
        return {"error": 404, "detail": "Runtime session not found"}
    session = crud_runtime_session.get_account_session(
        db, account_id=str(user.account_id), runtime_session_id=runtime_session_id
    )
    if session is None:
        return {"error": 404, "detail": "Runtime session not found"}
    if execution_id is not None:
        try:
            uuid.UUID(execution_id)
        except ValueError:
            return {"error": 404, "detail": "Flow execution not found"}
        execution = crud_flow_execution.get(
            db, execution_id, account_id=str(user.account_id)
        )
        if execution is None:
            return {"error": 404, "detail": "Flow execution not found"}
    try:
        approvals_visible = _approval_read_allowed(current_user=user, db=db)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        approvals_visible = False
    return {
        "runtime_session_id": str(session.id),
        "account_id": str(user.account_id),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "approvals_visible": bool(approvals_visible),
    }


def _audit_session_attach(
    db: Session,
    *,
    action: str,
    user_id: object,
    account_id: str,
    runtime_session_id: str,
    details: dict,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> None:
    from preloop.models.crud import crud_audit_log

    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action=action,
        resource_type="runtime_session",
        resource_id=runtime_session_id,
        status="success",
        ip_address=ip_address,
        user_agent=user_agent,
        details=details,
    )


@router.websocket("/ws/runtime-sessions/{runtime_session_id}")
async def runtime_session_websocket(websocket: WebSocket, runtime_session_id: str):
    """Stream one runtime session's live events to an attached client.

    Authentication: ``Authorization: Bearer <token>`` on the upgrade request.

    Query parameters:
        execution_id (optional): also stream events of this flow execution.
        read_only (optional): recorded on the audit event; enforcement of a
            read-only attach is the REST endpoints' permission checks.

    Messages sent: one ``attached`` message, then every account realtime
    event of the session exactly as the unified socket would deliver it.
    Messages accepted: ``{"type": "ping"}``, answered with ``pong``.
    """
    await websocket.accept()

    async def refuse(code: int, error: str, detail: str) -> None:
        try:
            await websocket.send_json(
                {"type": "error", "error": error, "detail": detail}
            )
            await websocket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            pass

    token = _bearer_token(websocket)
    user = await _resolve_token_user(token) if token else None
    if user is None:
        await refuse(
            SESSION_ATTACH_CLOSE_UNAUTHORIZED,
            "unauthorized",
            "A valid bearer token is required",
        )
        return

    execution_id = websocket.query_params.get("execution_id") or None
    read_only = websocket.query_params.get("read_only") in {"1", "true", "yes"}
    client_ip = get_client_ip(websocket)
    user_agent = websocket.headers.get("user-agent", "")

    try:
        decision = await run_db_async(
            lambda db: _authorize_session_attach(
                db,
                user=user,
                runtime_session_id=runtime_session_id,
                execution_id=execution_id,
            )
        )
    except Exception:
        logger.warning("Session attach authorization failed", exc_info=True)
        await refuse(
            SESSION_ATTACH_CLOSE_FORBIDDEN,
            "forbidden",
            "Session access could not be verified",
        )
        return
    if decision.get("error") == 403:
        await refuse(SESSION_ATTACH_CLOSE_FORBIDDEN, "forbidden", decision["detail"])
        return
    if decision.get("error") == 404:
        await refuse(SESSION_ATTACH_CLOSE_NOT_FOUND, "not_found", decision["detail"])
        return

    session_id = decision["runtime_session_id"]
    account_id = decision["account_id"]
    audit_details = {"execution_id": execution_id, "read_only": read_only}

    async def audit(action: str) -> None:
        try:
            await run_db_async(
                lambda db: _audit_session_attach(
                    db,
                    action=action,
                    user_id=user.id,
                    account_id=account_id,
                    runtime_session_id=session_id,
                    details=audit_details,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
            )
        except Exception:
            logger.warning("Session attach audit failed (%s)", action, exc_info=True)

    connection_id = await manager.connect_with_account(websocket, account_id)
    manager.session_streams[connection_id] = SessionStreamFilter(
        runtime_session_id=session_id,
        execution_id=execution_id,
        approvals_visible=decision["approvals_visible"],
    )
    await audit(SESSION_ATTACH_AUDIT_ATTACHED)
    try:
        await websocket.send_json(
            {
                "type": "attached",
                "runtime_session_id": session_id,
                "execution_id": execution_id,
                "ended_at": decision["ended_at"],
                "approvals_visible": decision["approvals_visible"],
            }
        )
        while True:
            try:
                text = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect(connection_id)
        await audit(SESSION_ATTACH_AUDIT_DETACHED)
