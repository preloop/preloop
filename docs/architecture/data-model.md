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

These changes cover the admission, authentication, summary, and refresh paths
described above. Gateway preparation still needs a separate connection-lifetime
change before long provider streams, and the agent-control WebSocket still needs
worker-owned per-message sessions. Those broader lifecycle changes are not implied
by moving individual authentication calls into workers.
