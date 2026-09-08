# preloop.models

`preloop.models` is the data layer: SQLAlchemy, Pydantic, CRUD, and Alembic. This chapter covers models, PostgreSQL + PGVector, the schema, and backend project layout.

## preloop.models (`./backend/preloop/models`)
*   **Purpose:** Data modeling and database interaction layer.
*   **Current Agent/Model Shape:** `AIModel` remains the durable flat row for provider, model identifier, endpoint, and credential reference, while `ManagedAgentAIModelBinding` carries explicit per-agent config slots and primary/default selection.
*   **Deferred Normalization:** Full provider-profile normalization is intentionally deferred until agent UX and policy semantics for many-model agents stabilize; the current migration keeps compatibility fields and avoids a broader schema split.
*   **Cost Analytics Shape:** `ApiUsage` records the measured usage event. Account-scoped pricing metadata should be stored separately from `AIModel` so the same provider/model can have different cost estimates per account, contract, currency, effective date, or self-hosted deployment. Credits, promotions, invoice-grade adjustments, and chargeback rules belong in Enterprise plugin models.
*   **Technology:** SQLAlchemy for ORM, Pydantic for data validation/schemas.
*   **Database:** Defines schema for PostgreSQL, including tables for organizations, projects, issues, embeddings, etc.
*   **Vector Store:** Integrates with PGVector for storing and querying issue embeddings.
*   **Operations:** Provides CRUD (Create, Read, Update, Delete) functions for all database entities.
*   **Migrations:** Uses Alembic for database schema evolution.

## Backend Project Structure

The backend codebase is organized to separate concerns between the data models, synchronization logic, and the API server.

*   **`backend/preloop/models/`**:
    *   **`models/`**: SQLAlchemy models defining the database schema (e.g., `issues.py`, `projects.py`).
    *   **`schemas/`**: Pydantic models for data validation and API I/O.
    *   **`crud/`**: Database access operations (Create, Read, Update, Delete).
    *   **`db/`**: Database connection and session management.
    *   **`alembic/`**: Database migration scripts.

*   **`backend/preloop/sync/`**:
    *   **`scanner/`**: Core logic for polling trackers and processing data.
    *   **`trackers/`**: Client implementations for different issue trackers (Jira, GitHub, GitLab).
    *   **`embeddings/`**: Logic for generating vector embeddings from issue text.
    *   **`scheduler/`**: Task scheduling logic for regular synchronization.
    *   **`worker/`**: Worker process logic for consuming tasks from NATS.

*   **`backend/preloop/api/`**:
    *   **`endpoints/`**: API route definitions grouped by resource.
    *   **`auth/`**: Authentication logic and router.
    *   **`app.py`**: FastAPI application entry point.

## Database (PostgreSQL + PGVector)
*   **Role:** Central data store for metadata and vector embeddings.
*   **Managed by:** `preloop.models` module.
*   **Key Features:** Relational data storage, efficient vector similarity search via PGVector.

## Database Schema (Managed by preloop.models)

The detailed schema is defined using SQLAlchemy models within the `preloop.models` directory. Key tables include:

*   **Organizations:** Stores organization metadata, settings, and potentially user associations.
*   **Projects:** Contains project details, tracker configurations (type, API URL, credentials), and links to organizations.
*   **Trackers:** Holds specific tracker instance details and encrypted credentials.
*   **Issues:** Stores core issue data (ID, title, description, status, labels, etc.) synchronized from trackers.
*   **Issue Embeddings:** Contains vector embeddings (using PGVector `vector` type) linked to issues, used for similarity search.
*   **Other Metadata:** Tables for comments, users, API keys, etc., as needed.

Schema migrations are managed using Alembic within `preloop.models`.


## Transactions and asynchronous request handling

Keep synchronous CRUD and password/credential work off the API event loop.
Synchronous FastAPI handlers and dependencies already execute in workers. Async
callers can use `preloop.api.loop_safety.run_db_off_loop` for a complete, sequential
unit of database work. Do not let cancellation close a session while that worker
still uses it: the helper drains the worker before propagating cancellation,
including repeated asyncio cancellation and AnyIO cancellation scopes. Provider
calls must have their own I/O timeout because draining can exceed a coroutine
deadline.

Avoid keeping database connections while waiting for a human. Native permission
checks resolve authentication into immutable scalar fields in a worker-owned
session, close that session, then await the approval. Read response DTOs while
their session is open; an ORM object that survives rollback can have expired
attributes even when `expire_on_commit=False`.

Account halt/admission and artifact quota transactions use PostgreSQL
`FOR NO KEY UPDATE`. This still excludes competing owners while allowing the
`KEY SHARE` foreign-key checks performed by independent child audit and usage
inserts. This choice assumes the transaction does not change the referenced
account identity. Other row locks protect distinct invariants and must not be
weakened mechanically. Heartbeats and operator lifecycle transactions both
update the managed-agent row before the runtime-session row.

OAuth credential refresh must serialize single-use refresh tokens. Its CRUD
helper reloads the locked row with `populate_existing` so an earlier identity-map
read cannot win over a peer's committed rotation. If rotation is no longer needed,
only the helper's savepoint is rolled back to release its new lock, leaving
caller work uncommitted and intact. A necessary refresh retains exclusion through
the existing rotation transaction; its provider request has a bounded timeout.

Use independent PostgreSQL sessions and bounded lock timeouts to test lock
compatibility and ordering. Include a live event-loop task in blocking-I/O and
cancellation tests; mock-only session tests do not exercise these failure modes.
Monitor lock waiters, transaction age, pool checkout pressure, and event-loop
latency separately from CPU and memory. Increasing a pool cannot resolve a lock
cycle.

HTTP model gateway generation routes explicitly own their database session.
OpenAI Chat/Responses, Anthropic Messages and Gemini generation release each
preparation transaction before ordinary provider calls, stream reads, retry
backoffs, policy detector work and approval waits. Model/auth scalar values and
credential relationships are materialized before detachment. Preparation writes
are committed rather than silently discarded. Later policy and usage work opens
fresh transactions; accounting always closes its transaction in cleanup. Internal
replay/optimization callers default to caller-owned sessions and retain their
existing transaction contract; they must manage their own external-I/O boundary.

OAuth refresh remains an intentional exception: single-use token rotation holds
its secret-row lock across the existing bounded 30-second refresh HTTP call and
commits the rotated grant before releasing. It runs in the gateway worker using
the same session, so this does not reserve an additional request connection.
Removing the lock would allow concurrent refreshes to invalidate the grant.

A nested runtime summary preserves its caller's usage/session scalar values
across its own provider wait. Summary schema introspection reuses the session's
current checkout instead of attempting to reserve a second pool connection.
Stream teardown closes the source iterator and flushes deferred bookkeeping in
a cancellation-protected worker, including ASGI 2.3 immediate disconnect after
the final body. It drains any active synchronous stream pull before closing the
iterator; cancellation can therefore wait for the existing upstream read timeout.
The terminal body is still sent before deferred success accounting. Gemini closes
its nested stream explicitly so partial usage is recorded before request cleanup.


## Connection-hold diagnostics

Request sync and async engines enable `DB_POOL_HOLD_DIAGNOSTICS=true` by default.
Each API and dedicated gateway process observes its own engines; the separate
health engine is excluded. Set the flag to `false` and restart to disable listener
installation and callsite capture. `DB_POOL_HOLD_STACKS=true` increases acquisition
signatures from three to eight application frames; the walk stops after 64 frames,
including SQLAlchemy greenlet parents for async callers.

The existing pool monitor warning includes the five oldest active holds with
monotonic durations and sanitized package-relative file/function/line identities.
At most 128 active holds are retained per engine. No SQL, parameters, source lines,
locals, credentials, account identifiers, frame objects or DBAPI connections are
captured. Metadata is removed on checkin, invalidation, close and detach; engine
disposal clears it and rebinds tracking to the replacement pool.

A saturated pool also retains at most five completed holds that lasted at least
five seconds and spanned saturation, for at most five minutes. This lets the next
monitor tick report acquisition evidence when an event-loop stall hid the active
incident. Warnings retain the existing monitor interval (30 seconds by default);
there is no extra watchdog or per-checkout log. `DB_MONITORING_ENABLED=false` stops
periodic monitoring; disable the diagnostics flag separately to stop collection.

These signatures identify where a connection was acquired, not its current wait
stack or a proven incident cause. Collection is bounded and best effort. Missing
frames or untracked checkouts do not establish that a path released its connection.
