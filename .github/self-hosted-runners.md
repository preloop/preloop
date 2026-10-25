# Self-hosted runners

CI prefers GitHub-hosted `ubuntu-latest`. General self-hosted runners provide
optional backend overflow when recent repository runs show hosted jobs queued
without a runner for at least two minutes. This is a queue-pressure signal,
not a measurement of GitHub's global capacity. The picker inspects up to five oldest queued and five oldest in-progress
runs from bounded 100-run API responses, plus the current run (deduplicated), bounds each API request, and leaves every shard hosted when
pressure or inventory cannot be read, the read token is absent, or
`CI_SELF_HOSTED_TESTS=false`. Fork and Dependabot PRs always remain hosted.
Trusted same-repository PRs and pushes may use overflow.

Overflow requires **all** of `self-hosted`, `Linux`, `X64`, and
`preloop-ci-general`, online and idle at selection time. Never give
`preloop-ci-general` to the dedicated `gce-tests-only` pool. That pool is
explicitly excluded from inventory even if it also carries default labels.
Apply the readiness label only after checking runner acquisition, Ubuntu
Python setup, writable workspaces, Docker access, and PostgreSQL/pgvector.
Ubuntu 24.04 uses setup-python's Ubuntu build when system Python is not 3.11;
it does not need a misleading `ubuntu-latest` label or a hosted toolcache.

The picker assigns at most one overflow shard per observed idle runner,
at most the number of delayed hosted jobs, and never more than half the
backend matrix. Other jobs and at least half the backend shards stay hosted.
This snapshot is not a reservation: another run can consume the runner or a VM
can stop after selection. GitHub does not automatically move an already queued
self-hosted job to hosted, and job timeouts do not bound queue time. If that
happens, disable overflow with `CI_SELF_HOSTED_TESTS=false`, cancel the stalled
run, and rerun it on hosted capacity. There is no VM provisioning in this flow.

Backend CI prefers a Postgres that is already listening on the runner. The
job creates a database named `preloop_ci_<run>_<attempt>_<shard>`, migrates
it, and drops that database at the end. It does not stop the server.

GitHub-hosted `ubuntu-latest` has no Postgres, so the same step starts
`pgvector/pgvector:pg16` on `127.0.0.1:5432` and removes that container
afterwards. A self-hosted VM that already runs the server skips that start.

## Bootstrap routing

Set `CI_SELF_HOSTED_BOOTSTRAP=true` to run changed-path detection, the runner
picker and the final CI aggregator on `preloop-ci-general` for trusted
same-repository PRs and pushes. Forks and Dependabot keep hosted routing.
This avoids waiting for hosted capacity before selecting idle overflow
runners. The general pool must provide `gh`, `jq`, Python 3 and coreutils;
changed-path detection also requires a writable checkout workspace.
`CI_SELF_HOSTED_TESTS=false` disables this routing as well as shard overflow.
If the pool goes offline, disable the bootstrap variable and rerun; GitHub
does not migrate an already queued assignment automatically.

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

## Workspace ownership

Backend shards run on the host as the runner user. Anything root-owned
under `runnerN/_work` (left by the old job container, or by a manual root
command) makes `actions/checkout` fail with EACCES. The backend job
reclaims ownership before checkout using `sudo -n` when the runner user has
passwordless sudo, otherwise `docker run` with the cached
`pgvector/pgvector:pg16` image (the runner user must be in the `docker`
group). If neither works, the step fails with
`still not owned by runner user`. Recover on the VM:

    sudo find /home/github/runner*/_work ! -user github -print -quit
    sudo chown -R github:github /home/github/runner*/_work
    sudo find /home/github/runner*/_work ! -user github | wc -l   # expect 0

## Isolated execution host tests

`gce-tests-only` is the registration label for the repository-scoped isolated
test pool. Its runners must never carry `preloop-ci-general`; the backend
overflow selector also explicitly rejects `gce-tests-only`, whether or not
the runner carries default labels. Only a manual dispatch of
`fuzz.yml` with `execution_host_tests: true` selects this pool; PRs and schedules
keep their existing hosted routing.

The pool runs each assignment in a fresh restricted Kubernetes pod with a new
workspace, a tokenless service account, and no host Docker socket, kubeconfig,
cloud identity, or deployment credentials. A serialized controller reuses only
the repository runner identity and starts `run.sh --once` for each pod. These
tests yield to hosted flow executions and use a reduced fuzz worker budget.

## Backend collection and CPU budgets

`scripts/run_backend_shard.py` discovers test files from the pytest configuration
without importing them, then assigns whole files to contiguous, duration-weighted
shards. New files receive an estimated weight and are always included. Pytest
collects only the assigned files; tests within a file retain their normal order.
The duration file is a balancing hint, not an allowlist. Coverage remains enabled
on all shards and the combined job still enforces the coverage floor.
Discovery honors static `collect_ignore` and `collect_ignore_glob` declarations in
in-scope `conftest.py` files, path-qualified `norecursedirs`, and virtualenv
directories. Dynamic `pytest_ignore_collect` hooks or computed ignore lists are
unsupported and fail selection, because honoring them would require importing
conftest code before sharding.

Inspect a shard without running tests:

```bash
python scripts/run_backend_shard.py --shards 18 --group 1 --plan
```

One runner slot is not necessarily one CPU: CodeQL and other analysis tools may
start multiple threads. Set CPU budgets on the host runner services and leave
headroom for PostgreSQL and the operating system. Use measured concurrent load,
not the number of registrations, to size the pool. Tool thread settings are
hints; the host CPU budget enforces isolation without changing workflow checks.

## Discovery fixture isolation

`discovery-parity.yml` runs Linux discovery fixtures without external network
access. GitHub-hosted runners use `sudo unshare --net` directly. Self-hosted
runners instead grant passwordless sudo only for the root-owned
`/usr/local/sbin/preloop-ci-offline` helper. The helper creates a network
namespace, enables loopback for local HTTP fixtures, forces
`PRELOOP_DISABLE_TELEMETRY=true` and `PRELOOP_DISCOVERY_OFFLINE=true`, then
drops to the invoking runner UID and GID with `setpriv` before executing the
test binary. Do not grant unrestricted sudo or direct `unshare` access to the
runner account. A missing helper or denied sudo must fail the check.

## Python dependency reuse

The `install-ci-python-deps` composite uses pinned uv to install hash-checked
locks into a fresh job-local environment. Persistent runners retain the extracted
package cache under their tool cache; hosted runners restore it from Actions
cache using the lockfile key. Self-hosted jobs skip archive upload/download and
cache pruning. Changed lockfiles are still validated on every install. Editable
project code is installed separately with `--no-deps` so each job tests its own
checkout. No mutable virtual environment is shared between jobs.

Install `patchelf` on minimal Linux runner images. When `setup-ci-python` falls
back to a downloaded interpreter, it verifies that Python can start without
`LD_LIBRARY_PATH` and repairs its tool-cache loader path if needed. The isolated
shell tests intentionally clear that variable, so a missing loader path must
fail before the test suite rather than produce unrelated test failures.
