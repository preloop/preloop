# Disposable Preloop capacity lab

This lab sends authenticated traffic through Preloop, PostgreSQL and NATS using a deterministic local model and MCP server. Each simulated agent connects to Preloop's MCP endpoint, discovers and calls the proxied `capacity_echo` tool, then consumes a streaming response through the separate model gateway. Independent probes exercise sign-in, authenticated control reads and liveness. The optional log lane publishes known-count execution logs through the real NATS `log-persisters` path.

It measures a particular checkout and workload. It does not run actual CLI agent processes, Kubernetes jobs, tracker PR reviews, human approvals or paid models. A synthetic agent's concurrency is therefore not a supported production agent count.

## Disposable VM quickstart

Use a new Linux VM with Docker Engine, Compose v2 and Python 3.11+. The example resource limits are in `compose.yaml`; a small VM starting point is 2 vCPU and 4 GiB RAM. The container memory ceilings total roughly 3.5 GiB while the load driver runs, leaving little OS headroom. Watch host pressure and lower ceilings or use a larger VM as needed. A local image build can need more memory and disk than the running lab: build on another machine of the same architecture and `docker save`/`docker load` the image if necessary. No VM is provisioned by these scripts.

From a checkout of the revision to test, the wrapper handles resource capture,
resolved Compose configuration, image records and service logs:

```bash
scripts/capacity/lab.sh up
scripts/capacity/lab.sh run --levels 1,2,4,8 --seconds 30 --logs-per-second 100
scripts/capacity/lab.sh down
```

The equivalent individual commands are:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
export CAPACITY_REVISION=$(git rev-parse HEAD)
docker compose -f scripts/capacity/compose.yaml build
# Use --wait so database migrations and service health pass before setup.
docker compose -f scripts/capacity/compose.yaml up -d --wait api gateway fake
mkdir -p scripts/capacity/artifacts
python3 scripts/capacity/collect.py --seconds 600 \
  --output scripts/capacity/artifacts/resources.jsonl &
collector_pid=$!
docker compose -f scripts/capacity/compose.yaml run --rm load \
  python -m scripts.capacity.run --levels 1,2,4,8,16,32 \
  --seconds 30 --logs-per-second 100 --cooldown-seconds 15
kill "$collector_pid" 2>/dev/null || true
docker compose -f scripts/capacity/compose.yaml logs --no-color \
  > scripts/capacity/artifacts/services.log
```

The run prints each stage's summary and creates a timestamped artifact directory. Exit 0 means the tested stages stayed inside the configured thresholds; exit 2 means a threshold was exceeded. An exception/setup failure is a harness failure, not a measured capacity limit. Keep the service logs when diagnosing either outcome. The bounded resource collector can finish by itself if a shell exits before killing it.

The Compose network is **internal**, has no published ports and cannot reach paid providers or public services. All credentials are disposable placeholders or randomly created local account keys. Do not attach this network to production services. The driver rejects external endpoint URLs, ignores proxy environment variables, and verifies the fixture identity. `collect.py` requires a local Unix-socket Docker context and the `preloop-capacity` project. Builds/pulls need Internet access; workload services do not. Nothing invokes a VM provider or deploys to staging/production.

To test a prebuilt application image, set `CAPACITY_IMAGE` to that image **only if it includes this checkout's `scripts/capacity` directory**, and skip `build`. The repository Dockerfile copies `scripts/` into the application image. Record the application commit and image digest; an image tag alone can move. Apply the harness patch to another checkout to compare versions; do not silently carry application fixes between candidates.

## Workloads and bounds

- `--levels 1,2,4,8` runs closed-loop concurrency steps. Connections are established per stage. Each actor waits for its tool and model response before the next operation. Setup latency is reported separately as `mcp_connect`.
- `--levels 16 --seconds 1800` is a soak. `--max-requests` and `--max-logs` bound retained evidence and offered work. A cap reached before the duration is a truncated observation, not a completed soak. Increase the caps deliberately and budget disk/memory.
- `--tool-delay-ms 1000` and `--think-ms 100` control tool hold time and actor pacing. Set `FAKE_MODEL_DELAY_MS`, `FAKE_TOKEN_DELAY_MS` and `FAKE_OUTPUT_TOKENS` before `up --force-recreate fake` to control time to first token, stream duration and response size. Model payloads are deterministic, and their token usage is synthetic.
- `--cancel-every 10` intentionally closes every tenth model stream after content begins. These count separately and do not count as completed model requests or dilute completion latency percentiles.
- `FAKE_ERROR_EVERY=10` injects fixture HTTP 503 responses. This is an error-recovery scenario; its threshold crossing is not a server capacity result. Set it back to zero before a capacity sweep.
- `--logs-per-second 100` enables a separate paced log lane, spread over `--log-executions 8`. The fixture helper uses models and CRUD to create a disabled flow and already-terminal executions in the newly registered account. No execution is queued, and no agent worker is enabled. Each line has a stage and sequence marker. The publisher reports offered and achieved rate; it never emits an unbounded catch-up burst. NATS flush confirms submission, not database persistence.
- `--cooldown-seconds 15` observes recovery after the producers stop. Separate recovery sign-in/control/ping samples and pool/queue snapshots remain in the artifacts. With logs enabled, bounded CRUD keyset pages reconcile submitted versus uniquely persisted rows and duplicates. Missing rows at the end of this window may still be delayed; inspect the recovery history or rerun with a longer window before calling them permanently lost.

Defaults stop at the first configured error-rate or p95 breach. `--continue-after-failure` continues only the explicitly bounded levels. Thresholds are `--max-error-rate` and `--max-p95-ms`; defaults are starting criteria, not product SLOs. A level with no successful tool or complete model samples also fails. A log persistence deficit, a recovery probe error/p95 breach, or missing successful recovery sign-in/ping samples also fails the stage. Recovery is unmeasured when its window is zero.

## Evidence and interpretation

`config.json` records workload settings, source revision and driver platform. `requests.jsonl` records operation, duration, status/error category and model time to first content, without tokens, request bodies or response payloads. `summary.json` reports stage throughput, successful full-request p50/p95/p99, all-attempt p99, error categories and first threshold crossing. It records attempted and completed throughput separately. Failed requests remain in error counts even if their latency is absent from successful-request percentiles.

`telemetry.jsonl` contains API/gateway health bodies (request pool occupancy and usage writer queue), NATS counters, recovery log counts and driver event-loop timer lag. Ping latency is an **indirect** signal of target event-loop responsiveness, not an instrumented server event-loop-delay gauge. Correlate it with driver lag and CPU saturation before blaming the target. Missing/erroring telemetry is evidence missing, never evidence of health. Health HTTP 200 alone does not establish available request-pool capacity.

`resources.jsonl` samples each lab container's Docker CPU, memory, process count and state, plus PostgreSQL connection/wait categories. Include the fake service and load driver: if either is saturated, the experiment has reached an injector/fixture limit. Service logs provide pool holder warnings, NATS write failures and restart diagnostics. The native smoke and unit tests validate behavior; they are not VM capacity benchmarks.

To identify what broke first, align timestamps around the first stage breach, check whether fixture errors were intentionally enabled, compare driver/fake resources with API/gateway resources, inspect PostgreSQL waits and request-pool occupancy, then check recovery sign-in and log reconciliation. A first failing step brackets a limit between the previous passing step and this step **for this workload**. Repeat narrower steps and multiple runs. Closed-loop traffic self-throttles when latency rises; it does not represent an unlimited open-loop arrival rate and cannot by itself establish a production capacity SLA.

Tune one variable at a time: application CPU/memory ceilings, request pool/overflow/timeouts (`CAPACITY_POOL_SIZE`, `CAPACITY_MAX_OVERFLOW`, `CAPACITY_POOL_TIMEOUT`), provider delay, log rate, or topology. Pool counts are per process/engine; keep the aggregate within PostgreSQL's connection budget. Increasing pool size cannot repair held connections. Capture resolved Compose configuration and image digests with the artifacts, then compare the same workload and thresholds.

## Cleanup

Stop collection, save artifacts, then remove only the lab project and its disposable database:

```bash
docker compose -f scripts/capacity/compose.yaml down --volumes --remove-orphans
```

The artifact directory remains on the host. Every run creates a new account, so recreate the volume for a clean baseline between comparisons. The lab does not modify application backend behavior. Local unit/protocol checks use the existing development environment:

```bash
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=.:backend \
  python -m pytest scripts/capacity/tests -q
```
