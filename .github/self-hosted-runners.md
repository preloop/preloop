# Self-hosted runners

Backend tests use GitHub-hosted `ubuntu-24.04` by default. Setting
`CI_SELF_HOSTED_TESTS=true` opts into the general pool when there is one idle
runner for every selected job, including the serial subprocess tests. A full
run starts six xdist shards and one serial job. An impact-selected run starts
only the groups it needs. All groups read one runner label from the picker.
Self-hosted xdist shards run two workers each.
Fork and Dependabot PRs always remain hosted. Trusted same-repository PRs and
pushes may use the general pool.

Reserve hosted-image labels for GitHub-hosted capacity: never attach
`ubuntu-latest` or `ubuntu-24.04` to a private runner. Label matching can route a
nominally hosted job to that private machine. This workflow uses `ubuntu-24.04`
for hosted jobs and retries, avoiding legacy private `ubuntu-latest` aliases;
keep `ubuntu-24.04` absent from private registrations.

General capacity requires **all** of `self-hosted`, `Linux`, `X64`, and
`preloop-ci-general`, online and idle at selection time. Never give
`preloop-ci-general` to the dedicated `gce-tests-only` pool. That pool is
explicitly excluded from inventory even if it also carries default labels.
Apply the readiness label only after checking runner acquisition, Ubuntu
Python setup, writable workspaces, Docker access, and PostgreSQL/pgvector.
Ubuntu 24.04 uses setup-python's Ubuntu build when system Python is not 3.11;
it does not need a misleading `ubuntu-latest` label or a hosted toolcache.

The picker assigns at most one shard per observed idle runner, up to the
configured general-pool limit. Runners are classified by label, never by name.
A runner registration represents
a configured slot, not a CPU; provision slots to fit the host's measured capacity.
This snapshot is not a reservation: another run can consume a slot or a spot
VM can stop after selection. GitHub cannot migrate an assigned job between
runner labels. The picker starts the hosted recovery workflow only when backend work is
actually assigned to private capacity. It watches trusted PRs, main pushes
and main nightlies for up to 30 minutes. There is no every-run requested
listener; failed completions retain a short fallback check. It releases its hosted runner as soon as the complete
backend matrix is visible and every optional-runner shard has succeeded or
all shards are assigned to hosted runners; unrelated hosted jobs can continue.
After a general-pool backend shard waits unassigned for three minutes, or its
assigned runner is reported offline/missing, it cancels the
current run and retries failed/canceled jobs and their dependents once on hosted
runners. A failed disk reserve check before any tests also permits the same
bounded hosted retry. Other completed failures without offline/missing-runner evidence stop
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

GitHub-hosted `ubuntu-24.04` has no Postgres, so the same step starts
`pgvector/pgvector:pg16` on `127.0.0.1:5432` and removes that container
afterwards. A self-hosted VM that already runs the server skips that start.

## Local pool (`preloop-ci-local`)

A Debian 12 VM with 8 CPUs and 15 GB of RAM hosts three runner slots,
deb1–deb3, labelled `self-hosted`, `Linux`, `X64` and `preloop-ci-local`.
Three slots on 8 CPUs leaves each job a little over two cores, which is what a
light job or one backend shard needs. Do not add slots without measuring:
more registrations on the same VM make every job on it slower. The earlier
deb4–deb12 registrations were removed.

The local pool is its own runner class. It is on by default for trusted
sources and is turned off with the repository variable
`CI_LOCAL_RUNNERS=false`; it does not need `CI_SELF_HOSTED_TESTS`.
`.github/scripts/pick-runners.sh` makes every decision from one inventory
snapshot and is covered by `backend/tests/test_github_ci_self_hosted_runner.py`:

- `Pick light-job runners` runs first and gives idle local slots to Detect
  changed paths, then to Lint & Format and CLI Fast (in that order; changes
  finishes before the other two start, so it can reuse a slot).
- `Pick test runner` runs after the fast gate. A run's backend groups go to
  the general pool when it has one idle runner per group, else to the local
  pool when it has one idle slot per group, else all to hosted. With three
  slots that means a narrow impact-selected set (one to three groups) can run
  locally; a full seven-group set stays hosted. Local slots left over go to
  the browser-fixture and fuzz leaves in that order. Fuzz runs two legs at
  once, so it needs two free slots or stays hosted. The docs leaf stays
  hosted: its lock needs Python 3.11.5 or later and Debian 12 ships 3.11.2.
- Anything without an idle slot at selection time, every picker failure,
  reruns, forks and Dependabot PRs use `ubuntu-24.04`. CI has no
  `pull_request_target` job.

The picker is defence in depth, not the security boundary. A job on a local
slot runs as a member of the `docker` group, which is root on that VM. For
`pull_request` events GitHub runs the workflow file from the PR's merge
commit, so a fork PR can name `preloop-ci-local` (or `preloop-ci-general`) in
its own `runs-on` and skip the picker. What stops that today is GitHub's
fork-workflow approval, which this repository sets to first-time
contributors only, and the repository-level runners sit in the default
runner group with no workflow restriction. Requiring approval for all
outside contributors, or moving the private runners into a runner group
restricted to workflows on `main`, would make the boundary hard.

The snapshot is not a reservation. A local job whose slot is taken in the
meantime waits for the next free slot. Backend shards on local slots are
watched by the hosted recovery listener like general-pool shards; light jobs
are not, and rely on their timeouts.

Each slot has its own `_work` directory. Between jobs a runner job-completed
hook (`ACTIONS_RUNNER_HOOK_JOB_COMPLETED`) deletes that job's workspace and
`_temp`, keeping the tool cache. An hourly systemd timer prunes Docker
containers, networks, images and build cache that no job is using. Chromium's
system libraries are preinstalled, because the slot user has no sudo and
`playwright install --with-deps` needs root.

## Hosted control jobs

The runner pickers and the final CI aggregator always run on GitHub-hosted
`ubuntu-24.04`. Changed-path detection prefers an idle local slot and falls
back to hosted whenever the light picker produced no answer. The general pool is optional spot capacity,
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
without importing them, then packs them into shards greedily, heaviest first, so
groups are not contiguous. A file heavier than a third of a shard is split at
test-node level: each owning shard runs the file and deselects the recorded
nodes of the other slices. Tests without history run, weighed at the median test
duration. The duration file is a balancing hint, not an allowlist. Each shard
runs pytest-xdist with `--dist worksteal`, one cloned database per worker
(`scripts/ci_postgres.py` makes `<job db>_gwN` from the initialised job
database and drops the clones afterwards). Coverage remains enabled on all
shards; the combined job enforces the coverage floor on whole-suite runs.

On pull requests and `main` pushes, `scripts/ci_select.py` first narrows the
inventory to the files the diff can affect. `scripts/ci_import_graph.py` parses
every `backend/preloop` and `backend/tests` module with `ast` (nothing is
imported) and a test file is selected when it loads a changed module through
module-level imports, crossing at most one function-local import. Changed test
files and the smoke set always run. Shared roots (models, CRUD and migrations,
`config.py`, any `conftest.py`, packaging and lock files, `.github/**`,
`scripts/ci_*.py`) select everything. The selection decides the shard count:
enough shards to keep each about as long as a whole-suite shard, from 1 to 6.
`plan --selection ci-selection.json` packs only those files. Version tags, the
`ci:full` label, a full rerun, stacked PRs, the merge queue, the nightly
schedule and manual runs use all 6 shards. The `ci-selection` artifact
records the files, the reason and the count.
Discovery honors static `collect_ignore` and `collect_ignore_glob` declarations in
in-scope `conftest.py` files, path-qualified `norecursedirs`, and virtualenv
directories. Dynamic `pytest_ignore_collect` hooks or computed ignore lists are
unsupported and fail selection, because honoring them would require importing
conftest code before sharding.

Inspect a shard without running tests:

```bash
python scripts/run_backend_shard.py plan --shards 6 \
  --durations .github/backend-shard-durations.json --out plan.json
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

## Token permissions and Scorecard

The recovery listener defaults to `contents: read`; only its trusted `recover`
job receives `actions: write` and `pull-requests: read`. Closed-PR cancellation
also needs job-scoped Actions write. Removing that permission breaks the API
operations; substituting a long-lived PAT does not reduce privilege.

Scorecard 5.6 flags those job-scoped grants, and its release allowlist does not
recognize `softprops/action-gh-release`. The tag-only release publisher requires
Contents write to create the draft, attach verified assets and publish it. Keep
that grant on the publishing job. VirusTotal only submits binaries now and has
read-only Contents permission; it no longer edits release notes. These changes
remove unnecessary access but do not make all Token-Permissions findings vanish.
The remaining grants require a documented security exception or a separately
provisioned GitHub App design, not an assertion that the scanner passed.

## Temporary environment cleanup

Backend jobs remove only their runner slot's `preloop-python` and `preloop-deps`
directories under `RUNNER_TEMP`, before checkout and after artifact upload even
when tests fail. The next job clears environments abandoned by abrupt runner
loss. Shared tool and uv caches, Docker images, volumes and other runner slots
are untouched. This bounds temporary environment retention; cache/image growth
still requires host maintenance with an exclusive maintenance window.

The duration hints are refreshed by the `backend-durations` job: every green
main-branch run converts its six JUnit reports to node durations with
`scripts/ci_refresh_durations.py` and saves them to the Actions cache. The
`backend-plan` job restores the newest entry (falling back to the committed,
file-level `.github/backend-shard-durations.json`, regenerated with
`--per-file`), packs files longest first, and fails
when the predicted largest shard exceeds 1.15x the mean. The Backend Coverage
job summarises the actual per-shard pytest seconds. Backend shards all run on
one runner class: self-hosted only when one idle general runner is available
per shard, otherwise hosted. Setup and queue time remain separate from test-time weights.
