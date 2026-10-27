# Self-hosted runners

CI distributes backend shards across available capacity at dispatch. Each
online idle general self-hosted runner takes one shard immediately, up to the
matrix size; the remaining shards use GitHub-hosted `ubuntu-latest`. There is
no hosted-queue delay threshold or fixed half-matrix cap. Unreadable inventory,
an absent read token, or `CI_SELF_HOSTED_TESTS=false` keeps every shard hosted.
Fork and Dependabot PRs always remain hosted. Trusted same-repository PRs and
pushes may use the general pool.

General capacity requires **all** of `self-hosted`, `Linux`, `X64`, and
`preloop-ci-general`, online and idle at selection time. Never give
`preloop-ci-general` to the dedicated `gce-tests-only` pool. That pool is
explicitly excluded from inventory even if it also carries default labels.
Apply the readiness label only after checking runner acquisition, Ubuntu
Python setup, writable workspaces, Docker access, and PostgreSQL/pgvector.
Ubuntu 24.04 uses setup-python's Ubuntu build when system Python is not 3.11;
it does not need a misleading `ubuntu-latest` label or a hosted toolcache.

The picker assigns at most one shard per observed idle runner, up to the
backend matrix size. Other jobs stay hosted. A runner registration represents
a configured slot, not a CPU; provision slots to fit the host's measured capacity.
This snapshot is not a reservation: another run can consume a slot or a spot
VM can stop after selection. GitHub cannot migrate an assigned job between
runner labels. The hosted recovery workflow watches trusted PRs and main pushes
for 30 minutes. After a general-pool backend shard waits unassigned for three
minutes, or its assigned runner is reported offline/missing, it cancels the
current run and retries failed/canceled jobs and their dependents once on hosted
runners. A completed failure without offline/missing-runner evidence stops
automatic recovery, including when it appears during cancellation. It waits
up to two minutes for cancellation to finish. Successful jobs are retained;
work in progress at cancellation may repeat. It never provisions a VM.

The watchdog also handles completed failed/timed-out backend jobs when their
assigned runner is offline/missing. This is an infrastructure inference, not
proof of why a test failed; the hosted retry must still pass the same checks.
Without the optional existing `CI_RUNNER_READ_TOKEN`, queue recovery works but
offline-runner inference is unavailable. API failures stop further mutations.
Second attempts, forks, Dependabot, closed/superseded PR heads, and user-canceled
runs are not automatically recovered. All backend retry attempts use hosted
capacity even when a successful picker output was retained by GitHub.

The listener runs trusted code pinned to its default-branch workflow commit,
with `actions: write` for cancellation/retry, `pull-requests: read` to verify
current PR heads, and `contents: read` for that code.
It never checks out the PR head or reads job logs. It activates only after
`ci-hosted-recovery.yml` and its script land on the default branch, and hosted
queue delays can delay the watchdog itself. Job queue/run transitions are not
atomic with API cancellation; the watchdog rechecks current head/state before
each mutation but cannot eliminate every cancellation race. If its bounded
watch ends or recovery fails, disable self-hosted routing with
`CI_SELF_HOSTED_TESTS=false`, cancel the stalled run, and rerun it manually.

Backend CI prefers a Postgres that is already listening on the runner. The
job creates a database named `preloop_ci_<run>_<attempt>_<shard>`, migrates
it, and drops that database at the end. It does not stop the server.

GitHub-hosted `ubuntu-latest` has no Postgres, so the same step starts
`pgvector/pgvector:pg16` on `127.0.0.1:5432` and removes that container
afterwards. A self-hosted VM that already runs the server skips that start.

## Hosted control jobs

Changed-path detection, the runner picker and the final CI aggregator always
run on GitHub-hosted `ubuntu-latest`. The general pool is optional spot capacity,
so an offline pool must not prevent the picker from selecting hosted shards or
the aggregator from finishing. `CI_SELF_HOSTED_BOOTSTRAP` is no longer used;
existing repository values have no effect on this workflow.

Only backend shards may use the general pool, under the capacity rules above.
`CI_SELF_HOSTED_TESTS=false` disables that routing. An offline or
empty runner inventory keeps all shards hosted without provisioning a VM.

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
capacity selector also explicitly rejects `gce-tests-only`, whether or not
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
