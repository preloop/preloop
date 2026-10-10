# Self-hosted runners

Backend CI prefers a Postgres that is already listening on the runner. The
job creates a database named `preloop_ci_<run>_<attempt>_<shard>`, migrates
it, and drops that database at the end. It does not stop the server.

GitHub-hosted `ubuntu-latest` has no Postgres, so the same step starts
`pgvector/pgvector:pg16` on `127.0.0.1:5432` and removes that container
afterwards. A self-hosted VM that already runs the server skips that start.

## Postgres

Run one pgvector container and leave it up. The image is a superuser, which
the migrations need for `CREATE EXTENSION vector`. Bind it to localhost so
it is not reachable off the machine:

```bash
docker run -d --name preloop-postgres --restart unless-stopped \
  -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=test_user \
  -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=postgres \
  pgvector/pgvector:pg16
```

A running `preloop-postgres` keeps its image if a later cleanup prunes
stopped containers. Do not reuse a name that starts with `preloop-ci-`.
That prefix belongs to a server a job started itself, named
`preloop-ci-<run>-<attempt>-<shard>`, and the job removes only that
container. The fallback listens on `15432 + shard` (15433 through 15450),
not on 5432, so two shards on one machine do not delete each other's
server. One runner process still takes one job; the separate ports cover
a VM that runs more than one.

If a job is cancelled before it can drop its database, the next job that
reuses this server drops `preloop_ci_*` databases whose files are older
than six hours.

Python on the VM must be 3.11 with venv. Backend shards run on the runner,
not inside `python:3.11-bookworm`, so `127.0.0.1:5432` is this server.
`actions/setup-python` has no Debian 12 build.

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv
python3.11 -m venv /tmp/preloop-python-check && /tmp/preloop-python-check/bin/python -V
```

The runner user must be allowed to talk to the local Docker daemon. The
fallback path, used only when nothing is listening on 5432, runs `docker`.

These environment variables override the defaults. Leave them unset when
the server above is what CI should use.

| Variable | Default |
| --- | --- |
| `PRELOOP_CI_POSTGRES_HOST` | `127.0.0.1` |
| `PRELOOP_CI_POSTGRES_PORT` | `5432` |
| `PRELOOP_CI_POSTGRES_USER` | `test_user` |
| `PRELOOP_CI_POSTGRES_PASSWORD` | `test_password` |
| `PRELOOP_CI_MAINTENANCE_DB` | `postgres` |

`PRELOOP_CI_POSTGRES_PORT` is the server to reuse. Set it only when the
persistent server is not on 5432. The per-shard fallback ports apply when
this stays at 5432 and nothing is listening. The password is also read
from `PGPASSWORD`, which the workflow sets, so the database URL does not
contain it.

## Check the server

From a checkout with the dev extra installed (`pip install -e ".[dev]"` so
`psycopg2` is importable):

```bash
docker inspect -f '{{.State.Running}}' preloop-postgres
# true

export PGPASSWORD=test_password
GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1 PRELOOP_CI_SHARD=99 \
  python scripts/ci_postgres.py prepare
```

The log line is `reusing Postgres at 127.0.0.1:5432`, and the URL ends in
`/preloop_ci_1_1_99`. A second Postgres container is not started:

```bash
docker ps --format '{{.Names}}' | grep preloop
# preloop-postgres
```

Drop only that database. The server keeps running:

```bash
python scripts/ci_postgres.py drop
pg_isready -h 127.0.0.1 -p 5432
# accepting connections
docker inspect -f '{{.State.Running}}' preloop-postgres
# true
```

`scripts/ci_postgres.py prepare` exits with an error if something is already
listening and then rejects `test_user`. It does not start a second server
in that case. Fix the role or the password (`test_password`) instead of
changing the port.

The unit coverage for the reuse decision is
`backend/tests/test_ci_postgres.py`. It does not need a database.
