# Preloop Development Guide

Only use the DB models defined in the preloop.models package `from preloop.models import models`
Do not access the DB directly in backend code. Always use the CRUD layer at `preloop.models.crud`

Use the Lit.dev framework for frontend code. If you create new web components ensure that the landing page content is not hidden in their shadow DOM.

## Commands
- **Activate venv**: `source .venv/bin/activate || source ../.venv/bin/activate`
- **Install**: `pip install -e ".[dev]"`
- **Run server**: `python -m preloop.server`
- **Run tests**: `pytest`
- **Run single test**: `pytest tests/path/to/test_file.py::TestClass::test_function`
- **Lint**: `ruff check .`
- **Format**: `ruff format .`
- **Type check**: `mypy backend tests`
- **Docker development**: `docker-compose up`
- **Install pre-commit**: `pre-commit install`
- **PostgreSQL access**: `docker compose exec postgres psql -U postgres -d preloop`
- **Database migrations**: `alembic upgrade head` (from backend/preloop/models)

## Git Workflow

- **NEVER use git push in any form unless explicitly requested by the user**
- After making changes, present them to the user for review before any git operations beyond committing locally
- After making significant changes, consider their impact on README.md and ARCHITECTURE.md and update these files accordingly.

## Code Style
- **Formatting**: Ruff format with 88 character line length
- **Imports**: Use isort with black profile, group stdlib/third-party/local
- **Types**: Use strict typing with mypy, all functions must have type annotations
- **Naming**: snake_case for variables/functions, PascalCase for classes, UPPER_CASE for constants
- **Error handling**: Use specific exceptions, log with appropriate level, handle async errors properly
- **Docstrings**: Google-style with type annotations, document params, returns, raises
- **Async**: Use async for I/O-bound operations, run_async utility for sync contexts
- **Testing**: All code changes should have corresponding tests. Use red/green TDD when possible.
- **Test telemetry**: ALL test scripts, rigs, and scripted runs must set `PRELOOP_DISABLE_TELEMETRY=true` (CLI, installers, and instance `.env`) so test traffic never pollutes funnel/adoption telemetry.

## Pre-commit Hooks
The project uses pre-commit hooks to ensure code quality. These hooks run automatically before each commit and include:
- Code formatting with ruff format
- Import sorting with isort
- Linting with ruff
- Various file checks (trailing whitespace, YAML validity, etc.)

To use pre-commit:
1. Install pre-commit: `pip install pre-commit`
2. Install the hooks: `pre-commit install`
3. The hooks will run automatically on git commit
4. To run hooks manually: `pre-commit run --all-files`
5. Activate venv before committing or running pre-commit

## Cursor Cloud specific instructions

This environment runs the dev stack **natively** (no Docker). System deps (PostgreSQL 16 + pgvector, NATS server, `python3.12-venv`, `libpq-dev`, `build-essential`) are baked into the VM snapshot, and the startup update script refreshes app deps (`.venv` via `pip install -e ".[dev]"` and `frontend` npm packages). PostgreSQL data (the `preloop` DB, migrations, and any seeded/created rows) persists in the snapshot — you normally only need to (re)start the processes below, not reinstall or re-migrate.

Services and how to start them (start each in its own tmux session):

| Service | Start command (from repo root) | Port | Notes |
|---|---|---|---|
| PostgreSQL 16 + pgvector | `sudo pg_ctlcluster 16 main start` | 5432 | Not auto-started on boot. User `postgres`/password `postgres`, DB `preloop`. |
| NATS (JetStream) | `nats-server -js -m 8222` | 4222 (mon 8222) | API/gateway **fail startup** without NATS. `-js` enables JetStream. |
| Backend API (role `all`) | `source .venv/bin/activate && ./start.sh --init-test-data` | 8000 | `start.sh` loads `.env`, runs `scripts/init_db.py --force`, then `python -m preloop.server`. Omit `--init-test-data` after first boot. |
| Frontend (Vite dev) | `cd frontend && npm run dev -- --host 0.0.0.0` | 5173 | Proxies `/api`, `/mcp`, `/openai`, `/anthropic` to `:8000`. Open the app here. |

Non-obvious gotchas:
- A gitignored `.env` at the repo root holds local dev config (`DATABASE_URL`, `NATS_URL`, `SECRET_KEY`, `PRELOOP_SERVICE_ROLE=all`, `PRELOOP_DISABLE_TELEMETRY=true`). Recreate it if missing; `start.sh` only exports vars not already set.
- Health/docs live under prefixes: liveness is `GET /api/v1/health` and API docs are `/docs/api` (there is no `/health` or `/docs`).
- `POST /api/v1/auth/register` (and the UI form) require a `username` in addition to `email`/`password`.
- Running `python -m preloop.server` with `PRELOOP_SERVICE_ROLE=all` serves both the API and the model gateway in one process — no separate gateway needed for local dev.
- Optional workers (`preloop-sync scheduler`, `preloop-sync worker`) are only needed for issue-tracker sync / flow execution, not for login/MCP/gateway smoke tests.
- Backend runs with `DEBUG=true`, so uvicorn hot-reloads on code changes; a full restart is only needed after dependency changes.
