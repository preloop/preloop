# Technical Decisions

This chapter records technical choices: FastAPI for the REST API, Python, PostgreSQL, and how the stack is deployed (Compose, Helm, service roles).

## REST API Implementation
Preloop implements a RESTful HTTP API using FastAPI, which provides:
- High performance with Starlette and Pydantic
- Automatic OpenAPI documentation generation
- Type annotation-based parameter validation
- Native async/await support
- Dependency injection system
- Middleware for authentication, logging, etc.

## Language and Framework
Python is chosen as the primary language due to its strong ecosystem for machine learning and data processing, which is essential for similarity search and embedding generation. FastAPI is used for the REST API due to its performance, type safety, and automatic OpenAPI documentation generation.

## Database
PostgreSQL with the PGVector extension is used. The `preloop.models` module encapsulates all database interaction logic, providing a clean separation from the API and synchronization services. This allows for centralized data management and schema evolution.

## Deployment
The system is designed to be containerized using Docker, enabling easy deployment in various environments including Kubernetes clusters. Stateless components enable horizontal scaling under load.

*   **Service roles:** `PRELOOP_SERVICE_ROLE` (`all` | `api` | `gateway`, default `all`) gates which subsystems boot in a given container: API-only deployments skip gateway-only surfaces and gateway-only deployments skip the MCP server, NATS WS consumer, execution monitor, plugins' API workers, and approval-repair passes. `create_app` lazy-imports API-only routers and MCP/auth so a dedicated gateway does not load the control-plane import graph. Compose already splits `api` and `gateway`. The Helm chart sets `PRELOOP_SERVICE_ROLE=api` and `PRELOOP_SERVICE_ROLE=gateway` on those Deployments so Kubernetes pods do not fall back to `all` and import the control plane into every gateway replica.
*   **Migrations:** `docker-compose.yml` and `docker-compose.release.yaml` run schema initialization in a dedicated one-shot `migrate` service that app services wait on (`service_completed_successfully`); Helm deployments run Alembic via their own lifecycle. `start.sh` still waits for `DATABASE_URL` to accept TCP before `init_db.py` for non-compose local runs.
*   **Dev compose:** `docker-compose.yml` healthchecks postgres and NATS and starts api/gateway/scheduler/worker only after both are healthy and `migrate` has completed. The Vite dev server honors `VITE_ALLOWED_HOSTS` when the console is reached by a public hostname.
*   **Health monitoring:** The Helm chart ships an optional in-cluster health-monitor deployment (`healthMonitor.*`, enabled by default) that polls `/api/v1/health` and logs alert lines after consecutive failures.
*   **Release verification:** `scripts/release_smoke_test.sh` boots the release compose file with tagged images and verifies HTTP health, first-user sign-up/login, and restart-loop-free stability; the release workflow runs it as the `verify-oss-install` gate before publishing a GitHub release.
*   **OSS installer `.env`:** `scripts/install-oss.sh` writes Compose `.env` values with `$` escaped as `$$` so secrets are not interpolated (and partial secrets are not leaked via compose WARNs). Hand-edits of `~/.preloop-oss/.env` need the same escaping.
