# Large-account Delivery Report verification

Use a development virtual environment and this checkout's backend. No customer
account, cluster, tracker, SMTP relay or model provider is needed for the launch
regression. The fixture supplies synthetic repositories and credentials.

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH="$PWD/backend" \
  .venv/bin/pytest backend/tests/agents/test_delivery_report_launch.py -q
```

To reproduce an account with 440 repositories alone:

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH="$PWD/backend" \
  .venv/bin/pytest backend/tests/agents/test_delivery_report_launch.py -k 440 -q
```

The tests build the actual Kubernetes Job and mounted Secret, and the private
Docker launch. They enforce the Linux per-string and combined argument/environment
budgets, serialized Kubernetes object sizes, Secret data size, and the private
runner's bootstrap and environment limits. A separate 500-repository fixture
uses distinct tracker credentials to catch credential-store expansion. Docker
upload ordering, file permissions, seed integrity and shell-state preservation
are exercised without starting a model. Nothing is deployed.

Native host profiles already receive checkout plans as data. Their Delivery
Report plans use a file for the repository path map and bypass the old eight-
repository plan limit. Run the native runner checks with:

```sh
(cd cli && PRELOOP_DISABLE_TELEMETRY=true go test ./internal/cmd \
  -run 'TestHostDeliveryReportWith500Repositories|TestHostExec' -count=1)
```

Native host runners need the CLI containing this change to accept reports with
more than eight active repositories. Existing smaller native reports and the
private Docker protocol remain compatible with older CLIs. Local development
CLI installation, if needed, must use `cd cli && make install-local`.

Against an isolated, migrated PostgreSQL test database, also run:

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH="$PWD/backend" \
  .venv/bin/pytest backend/tests/services/test_delivery_report_failures.py \
  backend/tests/endpoints/test_delivery_reports.py -q
```

These tests use synthetic accounts and a fake SMTP sender. They check activity
selection across 500 repositories, visible failures without result documents,
account isolation, opt-in recipient policy, persistent delivery outcomes, and
two concurrent workers competing for one notice claim. Set `DATABASE_URL` to
that isolated database; do not point it at a deployed instance.

The email claim permits one send attempt per execution. As with the existing
notification path, SMTP errors or a worker dying after the claim can leave an
unsent notice; the failure remains visible on the report page. Enterprise
custom roles and per-member recipient delivery are outside these OSS tests.

## A scheduled run, end to end

`backend/tests/services/test_delivery_report_local_run.py` seeds synthetic
accounts in that PostgreSQL database, runs the orchestrator's checkout
selection for the window Monday 07:00 UTC to the next Monday 07:00 UTC, and
builds the hosted Job and setup Secret for what it selected. Two shapes: 440
repositories, and 80 repositories (the size the earlier launch-budget fix was
tested at), each with a handful active and with every repository active.

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH="$PWD/backend" \
  .venv/bin/pytest backend/tests/services/test_delivery_report_local_run.py -q -s
```

Each case prints the repositories it cloned and the measured launch: about
58.5 KB of arguments and environment whatever the count, against the 1 MiB
budget.
