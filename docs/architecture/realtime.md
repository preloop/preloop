# Real-Time Communication

Preloop uses a single WebSocket per client with pub/sub routing. This chapter covers MessageRouter, topics, and the unified realtime architecture.

Preloop uses WebSocket connections for real-time updates:

## Unified WebSocket Architecture

Single WebSocket connection per client with pub/sub message routing:

**MessageRouter** (`backend/preloop/services/message_router.py`):
- Routes messages to topic-based subscribers
- Supports wildcard subscriptions (`'*'` topic)
- Optional per-subscriber filter functions
- Topics: `flow_executions`, `approvals`, `system`

**Benefits:**
- Single WebSocket reduces connection overhead
- Scalable pub/sub pattern
- Easy to add new message types/topics
- Clear separation of concerns

> **Enterprise Features**: Preloop Cloud and Preloop Enterprise add RBAC and approval workflows with quorum, escalations and AI gates. Contact sales@preloop.ai for more information.
