# Native development (no Docker)

Use this when the stack runs **on the machine** (a Linux VM, a spare host, or
Cursor Cloud) instead of `docker compose up`. Compose remains the default for
laptops that have Docker.

`Dockerfile.dev` at the repo root installs the same system packages. Cursor
Cloud builds it via `.cursor/environment.json`. Humans can build it and mount
the checkout:

```sh
docker build -f Dockerfile.dev -t preloop-native-dev .
docker run --rm -it -v "$PWD:/workspace" -w /workspace \
  -p 8000:8000 -p 5173:5173 -p 5432:5432 -p 4222:4222 \
  preloop-native-dev
```

The image does not copy the app. Cursor checks out the commit; a local run
must bind-mount the repo as above.

## System packages

- PostgreSQL 16 with pgvector
- NATS Server with JetStream
- Python 3.12 (`python3-venv`, `libpq-dev`, `build-essential`)
- Node.js 22

On Ubuntu 24.04 those are what `Dockerfile.dev` installs. On another distro,
install the same services and keep the ports below.

## One-time app install

From the repo root:

```sh
./scripts/native-dev-install.sh
```

That creates `.venv`, runs `pip install -e ".[dev]"`, installs `frontend`
npm packages, and writes a gitignored `.env` if one is missing. Re-run after
dependency changes; it is idempotent.

## Start the stack

Each process in its own terminal (or tmux session):

| Service | Start command (from repo root) | Port | Notes |
|---|---|---|---|
| PostgreSQL + pgvector | `./scripts/native-dev-deps.sh` | 5432 | User `postgres` / password `postgres`, DB `preloop`. The script also starts NATS. |
| NATS (JetStream) | (started by `native-dev-deps.sh`) | 4222 (mon 8222) | The API **fails startup** without NATS. |
| Backend API (`role=all`) | `source .venv/bin/activate && ./start.sh --init-test-data` | 8000 | `start.sh` loads `.env`, runs `scripts/init_db.py --force`, then `python -m preloop.server`. Omit `--init-test-data` after the first boot. |
| Frontend (Vite) | `cd frontend && npm run dev -- --host 0.0.0.0` | 5173 | Proxies `/api`, `/mcp`, `/openai`, `/anthropic` to `:8000`. Open the app here. |

PostgreSQL data (schema, migrations, seeded rows) lives on the host. After
the first boot you normally only restart processes, not reinstall or
re-migrate.

Optional workers (`preloop-sync scheduler`, `preloop-sync worker`) are only
needed for issue-tracker sync or flow execution, not for login, MCP, or
gateway smoke tests.

## `.env`

`start.sh` only exports variables that are not already set. A missing file
is recreated by `native-dev-install.sh` with:

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/preloop
NATS_URL=nats://localhost:4222
SECRET_KEY=dev-secret-not-for-production
PRELOOP_SERVICE_ROLE=all
PRELOOP_DISABLE_TELEMETRY=true
DEBUG=true
HOST=0.0.0.0
PORT=8000
```

`PRELOOP_SERVICE_ROLE=all` serves the API and the model gateway in one
process. No separate gateway is required for local dev.

## Gotchas

- Liveness is `GET /api/v1/health`. API docs are `/docs/api`. There is no
  `/health` or `/docs` on the API port.
- `POST /api/v1/auth/register` (and the UI form) require a `username` in
  addition to `email` / `password`.
- With `DEBUG=true`, uvicorn hot-reloads on code changes. Restart the
  process after dependency changes.
