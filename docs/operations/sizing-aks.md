# Reference sizing for Helm on AKS

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

!!! warning "Not load tested"
    Nothing on this page comes from a load test on Azure Kubernetes Service.
    Every number is either **from production shape** (a value in
    `helm/preloop/values.yaml`, its comments, or
    [Gateway memory](gateway-memory.md)), **derived** (arithmetic on those
    values, shown on this page), **Azure docs** (a cited Microsoft Learn
    page), or **estimate, not load-tested** (a choice we made without a
    measurement, labelled where it appears). There are no throughput
    figures here because we have none for AKS. Run the
    [load test recipe](#load-test-recipe) before you commit to a tier.

The three tiers are ready-made overlays in the chart:

| Tier | Overlay | Shape |
|---|---|---|
| Small | `helm/preloop/values-aks-small.yaml` | Chart default topology (one of each process), external Postgres |
| Medium | `helm/preloop/values-aks-medium.yaml` | The production shape the `database.pool` comment in `values.yaml` budgets for |
| Large | `helm/preloop/values-aks-large.yaml` | Production shape with more flow-execution workers and a higher gateway ceiling |

```bash
helm install preloop ./helm/preloop -f helm/preloop/values-aks-medium.yaml
```

Each overlay expects two Secrets you create first: `preloop-app` (key
`jwt-secret`, see [Application secrets](https://github.com/preloop/preloop/tree/main/helm/preloop#application-secrets))
and `preloop-db` (key `database-url`, the Flexible Server connection
string). Replace the example Postgres host.

## Choosing a tier

Tiers are defined by deployment shape and by how many hosted agents run at
once, not by requests per day. We have no measured requests-per-second for
one gateway replica, so any requests-per-day boundary would be invented.
Fill that column for your own workload with the
[load test recipe](#load-test-recipe).

| | Small | Medium | Large |
|---|---|---|---|
| Governed requests per day | not measured | not measured | not measured |
| Concurrent hosted agents (instance) | 10 | 30 | 50 |
| Concurrent hosted agents (one account) | 5 | 5 | 5 |
| API replicas | 1 | 2 | 2 |
| Gateway HPA min / max | 2 / 5 | 2 / 5 | 2 / 10 |
| Default worker replicas | 1 | 3 | 3 |
| Flow-execution worker replicas | 1 | 3 | 5 |

Where the numbers come from:

- **Concurrent hosted agents (instance)**, derived: flow-execution replicas
  times `flowExecution.maxInflight` (10, from production shape). Each
  flow-execution process monitors at most that many agent Jobs. The Large
  value, 50, equals `agentExecution.resourceQuota.maxJobs` (50), the
  chart's cap on concurrent agent Jobs; going past Large means raising that
  quota too.
- **Concurrent hosted agents (one account)**: `flowExecution.maxRunningPerAccount`
  (5, from production shape) caps one account on hosted compute. An
  instance with a single account never runs more than 5 hosted agents at
  once unless you raise that value or the per-account override. Agents
  running on laptops or private runners only send traffic through the
  gateway; they do not count here.
- **API 2, gateway 2 / 5, flow-execution 3**, from production shape: the
  `database.pool` comment ("production actually runs api=2, gateway HPA
  (min 2 / max 5), and several worker pools") and its budget line
  "flow-execution=3, other workers=5". We read "other workers=5" as the
  default pool (3) plus the scheduler (1) plus the monitor (1), which all
  use the `database.pool.worker` pool.
- **Small** keeps the chart defaults for every replica count.
- **Large flow-execution 5**, derived: the smallest count that reaches the
  `maxJobs` quota of 50 at `maxInflight` 10.
- **Large gateway max 10**: estimate, not load-tested. It is a
  deliberately higher ceiling than the chart default of 5. The chart
  README `autoscaling` example (the API/worker HPA) uses 10, while the
  gateway HPA is documented separately as min 2 / max 5. The comment in
  `values.yaml` says not to go above 5 without raising `max_connections`;
  that warning is about the in-cluster CloudNativePG default of 200, and
  the Flexible Server SKUs below allow far more (see
  [Connection budget](#connection-budget)).

The overlays also set `flowExecution.workerEnabled: true` so flow runs go
through the flow-execution pool, which is what the concurrency numbers
above assume. With it off, that pool idles and flows run inside the API
process.

## Pod resources and HPA

All tiers keep the chart's requests and limits (from production shape,
`values.yaml`):

| Component | Request | Limit | Pool (size + overflow) |
|---|---|---|---|
| API | 50m / 512Mi | 2 / 2Gi | 8 + 12 |
| Gateway | 100m / 768Mi | 1 / 2Gi | 6 + 14 |
| Default worker | 50m / 512Mi | 2 / 2Gi | 2 + 4 |
| Flow-execution worker | 50m / 512Mi | 2 / 2Gi | 10 + 4 |
| Scheduler | 50m / 128Mi | 500m / 512Mi | 2 + 4 |
| Monitor | 50m / 128Mi | 500m / 512Mi | 2 + 4 |
| Console | 50m / 64Mi | 200m / 256Mi | none |
| Health monitor | 10m / 16Mi | 50m / 64Mi | none |

Gateway HPA, all tiers (from production shape): CPU target 75%, memory
target 90%. The memory target is 90% of a 768Mi request, about 691Mi,
because idle gateway RSS on hosted clusters is 630-700Mi; a lower request
pins the HPA at `maxReplicas` while CPU is idle (see
[Gateway memory](gateway-memory.md#requests-and-limits)).

The API and worker HPA (`autoscaling.enabled`) stays off in every overlay.
Fixed replica counts are what make the connection budget below exact.

Totals for the Preloop pods, with the gateway at its HPA maximum (derived;
the NATS subchart sets no resources by default and is not included):

| Tier | CPU requests | Memory requests | CPU limits | Memory limits |
|---|---|---|---|---|
| Small | 810m | 5712Mi | 12250m | 17728Mi |
| Medium | 1060m | 8272Mi | 22250m | 27968Mi |
| Large | 1660m | 13136Mi | 31250m | 42304Mi |

## Node pools

Use two user node pools: one for the Preloop pods, one for agent Jobs.
Agent Jobs are bursty and can use up to 4Gi each; keeping them on their own
pool means a burst of agents cannot evict the gateway.

VM sizes are from the Dsv5 series
([Azure docs](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series)):
Standard_D4s_v5 has 4 vCPU and 16 GiB, Standard_D8s_v5 has 8 vCPU and 32 GiB.

**Preloop pool.** Rule (derived): with one node drained for an upgrade,
the remaining nodes still hold the sum of memory limits from the table
above, so `(nodes - 1) x VM memory >= memory limits`. Memory is the
constraint because a pod over its memory limit is killed, while a pod over
its CPU limit is only throttled. AKS keeps part of every node for itself, so
allocatable memory is below VM memory
([Azure docs](https://learn.microsoft.com/en-us/azure/aks/node-resource-reservations));
the rule uses VM memory and is therefore a floor.

| Tier | Memory limits | Minimum with D4s_v5 | Minimum with D8s_v5 | Suggested |
|---|---|---|---|---|
| Small | 17728Mi | 3 | 2 | 3 x Standard_D4s_v5 |
| Medium | 27968Mi | 3 | 2 | 2 x Standard_D8s_v5 |
| Large | 42304Mi | 4 | 3 | 3 x Standard_D8s_v5 |

**Agent pool.** Agent containers get the chart's LimitRange defaults
(from production shape): request 250m / 512Mi, limit 1 CPU / 4Gi. Run this
pool with the cluster autoscaler: the minimum node count covers the
requests of every concurrent agent, the maximum covers their limits
(derived, D8s_v5 nodes):

| Tier | Agents | Requests | Limits | Autoscaler min / max (D8s_v5) |
|---|---|---|---|---|
| Small | 10 | 2500m / 5Gi | 10 CPU / 40Gi | 1 / 2 |
| Medium | 30 | 7500m / 15Gi | 30 CPU / 120Gi | 1 / 4 |
| Large | 50 | 12500m / 25Gi | 50 CPU / 200Gi | 2 / 7 |

If no agents run as Jobs in the cluster (every agent runs on a laptop or a
private runner and only calls the gateway), skip the agent pool.

## PostgreSQL: Azure Database for PostgreSQL Flexible Server

Preloop needs PostgreSQL with pgvector. On Azure use Flexible Server and
set `database.external: true` with the URL in a Secret (the overlays do
this).

1. Allow-list the extension. Its name on Azure is `vector`, not `pgvector`
   ([Azure docs](https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-use-pgvector)).
   `parameter set` replaces the whole `azure.extensions` allow-list. Read
   the current list first with `SHOW azure.extensions;` and pass the full
   comma-separated value, keeping every extension already allow-listed
   ([Azure docs](https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-allow-extensions)):

    ```bash
    az postgres flexible-server parameter set \
      --resource-group <resource_group> \
      --server-name <server> \
      --name azure.extensions \
      --value "<existing>,vector"
    ```

2. Install the chart. The first migration runs
   `CREATE EXTENSION IF NOT EXISTS vector`, which succeeds once the
   extension is allow-listed.

### Connection budget

Each pod opens two SQLAlchemy pools plus one health-check connection (from
production shape, the `database.pool` comment):

- steady per pod = size x 2 + 1
- ceiling per pod = (size + maxOverflow) x 2 + 1

Applied to the tiers (derived), with the gateway at its HPA minimum and
maximum:

| Tier | Steady at gateway min | Ceiling at gateway min | Steady at gateway max | Ceiling at gateway max |
|---|---|---|---|---|
| Small | 79 | 191 | 118 | 314 |
| Medium | 148 | 316 | 187 | 439 |
| Large | 190 | 374 | 294 | 702 |

The Medium row matches the budget in the `values.yaml` comment. That comment
records a measured production peak of about 65 connections in total, so the
ceiling is deliberately oversubscribed against `max_connections`; the steady
total is what must fit.

Flexible Server sets `max_connections` from the SKU and keeps 15 slots
reserved, so user connections are `max_connections - 15`
([Azure docs](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits)):

| SKU (General Purpose) | vCores | Memory | Default max_connections | User connections |
|---|---|---|---|---|
| D2ds_v5 | 2 | 8 GiB | 859 | 844 |
| D4ds_v5 | 4 | 16 GiB | 1,718 | 1,703 |
| D8ds_v5 | 8 | 32 GiB | 3,437 | 3,422 |

Every tier's ceiling at gateway max fits inside D2ds_v5's 844 user
connections, so PgBouncer is not needed at these shapes. Two Azure details
from the same page: the default is fixed when the server is created, so
after changing SKU set `max_connections` yourself; and Microsoft advises
against raising it above the default.

Suggested SKUs:

| Tier | SKU | Why |
|---|---|---|
| Small | D2ds_v5 | Connection budget fits (derived). The chart's in-cluster database runs with a 1500m / 2Gi limit (from production shape); D2ds_v5 is larger than that. |
| Medium | D4ds_v5 | Estimate, not load-tested. |
| Large | D8ds_v5 | Estimate, not load-tested. |

We have no measurement of database CPU, memory or IOPS under load. Watch
the server's CPU and memory metrics and resize from those.

**Storage.** The smallest Flexible Server storage size is 32 GiB, the size
cannot be reduced later, the server goes read-only at 95% used, and
Microsoft suggests an alert at 80% and storage autogrow on Premium SSD
([Azure docs](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-storage)).
We have no growth-rate figure for Preloop data; start at 32 GiB with
autogrow on and watch the trend.

## NATS JetStream

Azure has no managed NATS service, so the overlays keep the bundled NATS
chart (`nats.enabled: true`) with JetStream on a file store. The PVC is
10Gi (the NATS chart's default) on the `managed-csi-premium` storage class,
which AKS creates for Premium SSD managed disks
([Azure docs](https://learn.microsoft.com/en-us/azure/aks/azure-csi-disk-storage-provision)).

All tiers run one NATS server. Preloop creates its `tasks` stream with
one replica and a 24 hour `max_age` (from production shape,
`backend/preloop/sync/services/nats_worker.py`), so a three-server NATS
cluster (`nats.config.cluster.enabled`) would not replicate that stream;
it would only add connection failover. The PVC is what keeps queued tasks
across a NATS pod restart.

## What to monitor

| Signal | Where | What it tells you |
|---|---|---|
| Gateway added latency, p95 | OTLP span durations (`otlp.enabled`, see [OTLP export](../guide/observability-otlp.md)) next to provider latency, or the capacity lab's `summary.json` | Overhead the gateway adds on top of the provider. Compare against your own baseline; we publish no target. |
| Gateway pod RSS against its 2Gi limit | `kubectl top pod`, container working set in Azure Monitor | Idle is 630-700Mi ([Gateway memory](gateway-memory.md)). Steady growth toward 2Gi means OOMKills are close. |
| Gateway HPA sitting at `maxReplicas` | `kubectl get hpa` | The tier is too small, or the memory request is below idle RSS again. |
| DB connections per pod and in total | The DB pool monitor logs a WARNING when a pod reaches 80% of its ceiling (`DB_MONITORING_ENABLED`, default on); Azure Monitor connection metrics for the server | Per-pod starvation (the failure mode in the `values.yaml` comment) versus a server-wide shortage. |
| JetStream lag on `tasks` | `nats stream info tasks` / `nats consumer report tasks` from a nats-box pod | Pending messages growing means workers cannot keep up. Messages older than 24 hours are dropped by `max_age`. |
| JetStream PVC usage | `kubectl get pvc`, Azure disk metrics | The 10Gi file store filling up. |
| Postgres storage percent | Azure Monitor | Alert at 80%; the server goes read-only at 95%. |
| Agent pool pending pods | `kubectl get pods -A --field-selector=status.phase=Pending` | The agent pool autoscaler maximum or the `maxJobs` quota is too low. |

## Load test recipe

The repository has a capacity lab in `scripts/capacity/` (see its README).
It runs Preloop, PostgreSQL and NATS in Docker Compose with a deterministic
fake model and MCP server, and sends authenticated MCP and streaming model
traffic in closed-loop concurrency steps. It does not deploy to AKS and
does not run real agent Jobs, so it measures what one set of processes can
carry, not a whole cluster.

1. Create a disposable Linux VM of the node size you plan to use (for
   example Standard_D4s_v5), with Docker Engine, Compose v2 and Python 3.11+.
2. From a checkout of the release you will deploy:

    ```bash
    export PRELOOP_DISABLE_TELEMETRY=true
    scripts/capacity/lab.sh up
    scripts/capacity/lab.sh run --levels 1,2,4,8,16,32 --seconds 60
    scripts/capacity/lab.sh down
    ```

3. Read `summary.json`: take the last level that passed and its completed
   model requests per second.
4. To estimate requests per day for a tier, multiply that rate by the
   gateway replica count you will run and by the seconds in your busy
   period, not by 86,400: agent traffic is not flat across a day. Label the
   result as your measurement.
5. Install the tier overlay on a non-production AKS cluster, replay your
   own traffic or the lab's workload against it, and watch the signals in
   [What to monitor](#what-to-monitor). A gateway HPA at `maxReplicas`, a
   pod near its memory limit or DB pool warnings mean the tier is too
   small.

Please send results back (an issue on preloop/preloop is fine) so this page
can carry measured numbers instead of production shape.
