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

## CLI dev builds

- The only sanctioned way to update a local dev CLI is `cd cli && make install-local` (builds, then `install -m 755 build/preloop ~/.local/bin/preloop`; `PREFIX`, `BINDIR`, `INSTALL_MODE` override the defaults).
- Never `cp` a build onto `~/.local/bin/preloop`. `cp` writes into the existing inode, which invalidates the macOS code-signature cache; the next exec dies with `SIGKILL (Code Signature Invalid)`. `install(1)` unlinks and recreates the file, which is what keeps that cache valid.
- Never `go build -o ~/.local/bin/preloop` either: it skips the version ldflags (the binary reports the compiled-in fallback version and looks like a release to the update check) and it replaces the file even when the target is read-only, so the guard below does not catch it.
- On macOS dev machines keep the binary guarded with `chmod a-w ~/.local/bin/preloop` (or install with `INSTALL_MODE=555`; the default 755 install drops the guard, so re-apply it). `make install-local` still works on a read-only target, a stray `cp` is refused, and `preloop update` honours the guard instead of replacing a dev build with the release.
- A dev build reports the `git describe` version (`v0.15.0-678-g5c9e8bc3`), which the CLI treats as newer than the `0.15.0` release; `preloop update --check` prints `newer than latest release` for it.

## Git Workflow

- **NEVER use git push in any form unless explicitly requested by the user**
- After making changes, present them to the user for review before any git operations beyond committing locally
- After making significant changes, consider their impact on README.md and ARCHITECTURE.md and update these files accordingly.

## Keep sample data generic
This repository is public, so anything written here is written for a general
audience. In commit messages, PR and issue text, comments, docstrings,
fixtures and changelog entries, prefer generic examples over data copied from
a real deployment:

- use `example.com` addresses and placeholder names such as `Jane Doe`
- describe a configuration by its shape ("a user configured an
  OpenAI-compatible provider"), not by whose it is
- use synthetic ids in fixtures rather than real account, tracker or project
  identifiers
- keep infrastructure sizing (replica counts, resource limits, database
  tuning) in deployment config and internal runbooks, not in prose

Generic examples read better anyway: they describe the case under test
instead of an anecdote the reader has no context for. Citing a public issue
number is fine and usually more useful than restating its background.

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
