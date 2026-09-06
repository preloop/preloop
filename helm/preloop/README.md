# Preloop Helm Chart

This Helm chart deploys Preloop, an event-driven automation platform with built-in human-in-the-loop safety.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.2.0+
- PV provisioner support in the underlying infrastructure (if persistence is enabled)
- PostgreSQL with PGVector extension (either deployed as part of this chart or externally)

## Installing the Chart

To install the chart with the release name `preloop`:

```bash
# Add the Preloop Helm repository (if available)
# helm repo add preloop https://charts.preloop.ai
# helm repo update

# Install the chart from local path
helm install preloop ./helm/preloop
```

The command deploys Preloop on the Kubernetes cluster in the default configuration. The [Parameters](#parameters) section lists the parameters that can be configured during installation.

## Private cluster

This section is the Kubernetes install path for a cluster with no public
LoadBalancer and with model traffic leaving Preloop only to endpoints you
already operate. **Docker Compose** (the OSS installer) and **this Helm
chart** are the supported install surfaces. This repository does not ship
Terraform modules.

A copy-paste overlay lives in [`values-private-cluster.yaml`](values-private-cluster.yaml).
Install with:

```bash
# Create secrets first (examples below). Then:
helm install preloop ./helm/preloop -f ./helm/preloop/values-private-cluster.yaml
```

OTLP export is off by default (`otlp.enabled: false`). See
[OTLP parameters](#otlp-parameters) and `docs/guide/observability-otlp.md`.

### Checklist

1. **Service**: keep `service.type: ClusterIP`. Do not require a public
   LoadBalancer. Reach the console and gateway through Ingress (or a mesh).
2. **Images**: override `image.repository` / `console.repository` to your
   registry and set `imagePullSecrets`.
3. **TLS**: terminate TLS on Ingress (or the mesh). The chart renders a
   console Ingress and a gateway Ingress (`/openai`, `/anthropic`, `/gemini`).
4. **Postgres**: use an existing server (`database.external: true`) and put
   `DATABASE_URL` in a Secret (`database.urlFromSecret`). In-cluster
   CloudNativePG (`database.enabled: true`, `database.external: false`) is
   for development.
5. **NATS**: in-cluster (`nats.enabled: true`) or an existing server
   (`nats.enabled: false` and `nats.url`). This chart does not deploy Redis.
6. **App secrets**: `SECRET_KEY` (jwt-secret) via `existingSecret` or a
   Secret the chart creates from values you pass at install time. Do not
   commit real keys to git. Create the first admin user in the console; do
   not put bootstrap passwords in values files.
7. **Private CA**: mount the CA with `extraVolumes` / `extraVolumeMounts`
   and set `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` in `extraEnv`.
8. **Egress** still required: your container registry, DNS, and (optional)
   public provider APIs if a model is not pointed at an internal upstream.
   An OTLP collector is optional and is not configured by this chart yet.

### ClusterIP and ingress

```yaml
service:
  type: ClusterIP
  port: 8000

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: private-ca-issuer
  hosts:
    - host: preloop.internal
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: preloop-tls
      hosts:
        - preloop.internal
```

TLS can also terminate at a service mesh. In that case leave `ingress.tls`
empty and configure the mesh separately; pods still listen on ClusterIP.

### Private registry

```bash
kubectl create secret docker-registry registry-pull \
  --docker-server=registry.internal \
  --docker-username=acme \
  --docker-password='<token>'
```

```yaml
image:
  repository: registry.internal/acme/preloop
  tag: "1.0.0"
  pullPolicy: IfNotPresent
console:
  repository: registry.internal/acme/console
  tag: "1.0.0"
imagePullSecrets:
  - name: registry-pull
```

### Existing Postgres

Prefer a Secret for the connection string (including `sslmode=verify-full`
and a password). The URL never belongs in git.

```bash
kubectl create secret generic preloop-db \
  --from-literal=database-url='postgresql://preloop:<password>@postgres.internal:5432/preloop?sslmode=verify-full'
```

```yaml
database:
  enabled: true
  external: true
  urlFromSecret:
    name: preloop-db
    key: database-url
  externalDatabase:
    host: postgres.internal
    port: 5432
    database: preloop
    sslMode: verify-full
```

`sslMode` is appended only when `urlFromSecret.name` is empty and the chart
builds `DATABASE_URL` from `externalDatabase.*`. For development, keep
`database.external: false` to deploy in-cluster CloudNativePG.

### Application secrets

```bash
kubectl create secret generic preloop-app \
  --from-literal=jwt-secret="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```yaml
existingSecret: preloop-app
environment:
  jwtSecret: ""
```

When `existingSecret` is set, the chart does not create a Secret. The named
Secret must contain `jwt-secret` (mapped to `SECRET_KEY`). Enable optional
features only if that Secret also has the matching keys (`encryption-key`,
`openai-api-key`, and so on).

AI model API keys are **not** Helm values. Store them as Preloop AI-model
secrets in the console (or the API) after install.

### Private CA for upstream TLS

Mount your CA and point Python TLS env vars at it. Completions and model
discovery then trust that bundle **for OpenAI-compatible (custom
`api_base`) upstreams**. Public OpenAI, Anthropic, OpenRouter, and
other cloud providers keep the default trust store (certifi / system
roots). An `openai-compatible` or `custom` model whose endpoint is
`https://openrouter.ai/api/v1` is treated as OpenRouter, not as a custom
upstream. httpx treats a `verify` path as the sole CA bundle, so a file
that contains only your internal CA would break those public APIs if it
were applied globally.

If a custom upstream and a public provider must share one file, the
bundle has to include public roots as well as your CA. Prefer keeping
the private CA for the custom endpoint only.

`PRELOOP_SSL_VERIFY=false` is the same scope: it skips verification on
custom OpenAI-compatible upstreams only, not on public clouds. Prefer a
mounted CA.

```bash
kubectl create secret generic private-ca --from-file=ca.crt=./acme-ca.crt
```

```yaml
extraVolumes:
  - name: private-ca
    secret:
      secretName: private-ca
extraVolumeMounts:
  - name: private-ca
    mountPath: /etc/ssl/private-ca
    readOnly: true
extraEnv:
  - name: SSL_CERT_FILE
    value: /etc/ssl/private-ca/ca.crt
  - name: REQUESTS_CA_BUNDLE
    value: /etc/ssl/private-ca/ca.crt
  - name: CURL_CA_BUNDLE
    value: /etc/ssl/private-ca/ca.crt
```

Those env vars are applied to API, gateway, workers, and jobs. They
change TLS only for OpenAI-compatible upstreams (and discovery against
those endpoints). As a last resort, `PRELOOP_SSL_VERIFY=false` skips
TLS verification on those custom upstreams; prefer a mounted CA.

### OpenAI-compatible upstream

Agents still authenticate to Preloop. They call Preloop `/openai/v1`
(and `/anthropic`, `/gemini`). Preloop then calls **your** OpenAI-compatible
endpoint as the model provider. There is no bypass-the-gateway mode.

In the console, open **AI models** and create a model:

1. Provider: **OpenAI-compatible**.
2. Endpoint (api base): `https://gateway.internal/v1`.
3. Model identifier: the id your upstream lists (for example `llama-3`).
4. API key: stored as a Preloop AI-model secret, not in Helm values.

A test completion through Preloop:

```bash
curl -sS https://preloop.internal/openai/v1/chat/completions \
  -H "Authorization: Bearer <preloop-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3","messages":[{"role":"user","content":"ping"}]}'
```

Preloop resolves that alias to the model record whose `api_endpoint` is
`https://gateway.internal/v1` and sends the provider request there.

### Resources (small production)

Chart defaults are sized for a small production instance. Raise them under
load with `--set` or a values overlay:

| Component | Default request | Default limit |
|-----------|-----------------|---------------|
| API | `50m` CPU / `512Mi` | `2` CPU / `2Gi` |
| Gateway | `100m` CPU / `256Mi` | `1` CPU / `1Gi` |
| Worker | `50m` CPU / `512Mi` | `2` CPU / `2Gi` |
| Console | `50m` CPU / `64Mi` | `200m` CPU / `256Mi` |

```bash
helm upgrade preloop ./helm/preloop \
  --set api.resources.requests.cpu=500m \
  --set api.resources.limits.memory=4Gi \
  --set gateway.resources.requests.memory=512Mi
```

Gateway HPA is on by default (`gateway.autoscaling`). API HPA is off until
you set `autoscaling.enabled: true`.

## Uninstalling the Chart

To uninstall/delete the `preloop` deployment:

```bash
helm uninstall preloop
```

## Parameters

### Common parameters

| Name                | Description                                                                                         | Value           |
|---------------------|-----------------------------------------------------------------------------------------------------|-----------------|
| `replicaCount`      | Number of replicas                                                                                 | `1`             |
| `image.repository`  | Preloop image repository                                                                        | `ghcr.io/preloop/preloop` |
| `image.tag`         | Preloop image tag                                                                               | `latest`        |
| `image.pullPolicy`  | Preloop image pull policy                                                                       | `Always`  |
| `imagePullSecrets`  | Secret names for pulling images                                                                    | `[]`            |
| `existingSecret`    | Existing Secret name for `jwt-secret` (chart skips creating its Secret)                            | `""`            |
| `extraEnv`          | Extra env vars on API, gateway, workers, and jobs                                                  | `[]`            |
| `extraEnvFrom`      | Extra envFrom entries on those pods                                                                | `[]`            |
| `extraVolumes`      | Extra volumes (for example a private CA Secret)                                                    | `[]`            |
| `extraVolumeMounts` | Extra volume mounts on those pods                                                                  | `[]`            |
| `nameOverride`      | String to partially override the name template                                                     | `""`            |
| `fullnameOverride`  | String to fully override the name template                                                         | `""`            |

### Service parameters

| Name                       | Description                                              | Value       |
|----------------------------|----------------------------------------------------------|-------------|
| `service.type`             | Service type                                             | `ClusterIP` |
| `service.port`             | Service port                                             | `8000`      |

### Ingress parameters

| Name                       | Description                                              | Value       |
|----------------------------|----------------------------------------------------------|-------------|
| `ingress.enabled`          | Enable ingress record generation                         | `false`     |
| `ingress.className`        | IngressClass that will be used                           | `""`        |
| `ingress.annotations`      | Annotations for the ingress record                        | `{}`        |
| `ingress.hosts`            | Hosts configuration for the ingress record               | See values.yaml |
| `ingress.tls`              | TLS configuration for the ingress record                 | `[]`        |

### Database parameters

| Name                               | Description                                           | Value       |
|------------------------------------|-------------------------------------------------------|-------------|
| `database.enabled`                 | Deploy PostgreSQL instance                            | `true`      |
| `database.external`                | Use external PostgreSQL instance                      | `false`     |
| `database.externalDatabase.host`   | External PostgreSQL host                             | `""`        |
| `database.externalDatabase.port`   | External PostgreSQL port                             | `5432`      |
| `database.externalDatabase.user`   | External PostgreSQL user                             | `""`        |
| `database.externalDatabase.password` | External PostgreSQL password                       | `""`        |
| `database.externalDatabase.database` | External PostgreSQL database                       | `""`        |
| `database.externalDatabase.sslMode`  | Optional libpq sslmode on a chart-built URL        | `""`        |
| `database.urlFromSecret.name`        | Secret containing DATABASE_URL                     | `""`        |
| `database.urlFromSecret.key`         | Key inside that Secret                             | `database-url` |
| `database.postgresql.auth.username` | PostgreSQL username                                 | `postgres`  |
| `database.postgresql.auth.password` | PostgreSQL password                                 | `postgres`  |
| `database.postgresql.auth.database` | PostgreSQL database                                 | `preloop` |
| `database.postgresql.service.port`  | PostgreSQL service port                             | `5432`      |
| `database.postgresql.persistence.enabled` | Enable PostgreSQL persistence                | `true`      |
| `database.postgresql.persistence.size`   | PostgreSQL persistence size                    | `1Gi`       |
| `database.postgresql.pgvector.enabled`   | Enable PGVector extension                      | `true`      |
| `database.cnpg.resilience.enabled`       | Enable database resilience settings (timeouts) | `false`     |
| `database.cnpg.resilience.statement_timeout` | Kill queries running longer than this (ms) | `300000`    |
| `database.cnpg.resilience.idle_in_transaction_session_timeout` | Kill idle transactions (ms) | `300000` |
| `database.cnpg.resilience.lock_timeout`  | Maximum time to wait for a lock (ms)           | `60000`     |
| `database.cnpg.logging.enabled`          | Enable slow query logging                      | `false`     |
| `database.cnpg.logging.log_min_duration_statement` | Log queries slower than this (ms) | `1000`      |
| `database.cnpg.queryAnalysis.enabled`    | Enable pg_stat_statements and auto_explain     | `false`     |
| `database.cnpg.queryAnalysis.autoExplainMinDuration` | Log EXPLAIN for queries slower than (ms) | `1000` |

### Environment parameters

| Name                           | Description                                           | Value       |
|--------------------------------|-------------------------------------------------------|-------------|
| `environment.host`             | Server host                                           | `0.0.0.0`   |
| `environment.port`             | Server port                                           | `8000`      |
| `environment.debug`            | Enable debug mode                                     | `false`     |
| `environment.jwtSecret`        | JWT secret key                                        | `change-this-in-production` |
| `environment.jwtAlgorithm`     | JWT algorithm                                         | `HS256`     |
| `environment.jwtExpireMinutes` | JWT expiration time in minutes                        | `60`        |
| `environment.logLevel`         | Log level                                             | `INFO`      |
| `environment.logFormat`        | Log format                                            | `json`      |
| `environment.skipExecutionRecovery` | Skip recovering orphaned executions on startup  | `false`     |

### OTLP parameters

Optional OpenTelemetry OTLP export for gateway completions and MCP tool calls.
Disabled by default. The same values are injected into the API and gateway
deployments. See `docs/guide/observability-otlp.md`.

| Name | Description | Value |
|------|-------------|-------|
| `otlp.enabled` | Enable OTLP export | `false` |
| `otlp.endpoint` | OTLP endpoint URL | `""` |
| `otlp.protocol` | `http/protobuf` or `grpc` | `http/protobuf` |
| `otlp.headers` | Header string `key=value,key2=value2` stored on the chart Secret | `""` |
| `otlp.headersSecret.name` | Existing Secret name for ingest headers (wins over `otlp.headers`) | `""` |
| `otlp.headersSecret.key` | Key inside that Secret | `otlp-headers` |
| `otlp.resource.serviceName` | Resource `service.name` | `preloop` |
| `otlp.resource.serviceNamespace` | Resource `service.namespace` | `""` |
| `otlp.resource.deploymentEnvironment` | Resource `deployment.environment` | `""` |
| `otlp.samplerRatio` | Parent-based TraceIdRatioBased sampler ratio in `[0, 1]` | `1.0` |

### Resource management parameters

Resources are configured per-component to allow fine-grained control:

| Name                              | Description                        | Default Value |
|-----------------------------------|------------------------------------|---------------|
| `api.resources.requests.cpu`      | API server CPU request             | `200m`        |
| `api.resources.requests.memory`   | API server memory request          | `256Mi`       |
| `api.resources.limits.cpu`        | API server CPU limit               | `1`           |
| `api.resources.limits.memory`     | API server memory limit            | `1Gi`         |
| `console.resources.requests.cpu` | Frontend (nginx) CPU request       | `50m`         |
| `console.resources.requests.memory` | Frontend memory request         | `64Mi`        |
| `console.resources.limits.cpu`   | Frontend CPU limit                 | `200m`        |
| `console.resources.limits.memory` | Frontend memory limit             | `256Mi`       |
| `worker.resources.requests.cpu`   | Worker CPU request                 | `200m`        |
| `worker.resources.requests.memory` | Worker memory request             | `256Mi`       |
| `worker.resources.limits.cpu`     | Worker CPU limit                   | `1`           |
| `worker.resources.limits.memory`  | Worker memory limit                | `1Gi`         |
| `scheduler.resources.requests.cpu` | Scheduler CPU request             | `100m`        |
| `scheduler.resources.requests.memory` | Scheduler memory request       | `128Mi`       |
| `scheduler.resources.limits.cpu`  | Scheduler CPU limit                | `500m`        |
| `scheduler.resources.limits.memory` | Scheduler memory limit           | `512Mi`       |
| `monitor.resources.requests.cpu`  | Monitor CPU request                | `100m`        |
| `monitor.resources.requests.memory` | Monitor memory request           | `128Mi`       |
| `monitor.resources.limits.cpu`    | Monitor CPU limit                  | `500m`        |
| `monitor.resources.limits.memory` | Monitor memory limit               | `512Mi`       |

**Example: Override API resources**

```bash
helm install preloop ./helm/preloop \
  --set api.resources.requests.cpu=500m \
  --set api.resources.limits.memory=2Gi
```

### Other parameters

| Name                           | Description                                           | Value       |
|--------------------------------|-------------------------------------------------------|-------------|
| `serviceAccount.create`        | Create a service account                              | `true`      |
| `serviceAccount.annotations`   | Annotations for the service account                   | `{}`        |
| `serviceAccount.name`          | The name of the service account                       | `""`        |
| `podAnnotations`               | Annotations for pods                                  | `{}`        |
| `podSecurityContext`           | Pod security context                                  | `{}`        |
| `securityContext`              | Container security context                            | `{}`        |
| `nodeSelector`                 | Node selector                                         | `{}`        |
| `tolerations`                  | Tolerations                                           | `[]`        |
| `affinity`                     | Affinity settings                                     | `{}`        |
| `autoscaling.enabled`          | Enable autoscaling                                    | `false`     |
| `autoscaling.minReplicas`      | Minimum number of replicas                            | `1`         |
| `autoscaling.maxReplicas`      | Maximum number of replicas                            | `5`         |
| `autoscaling.targetCPUUtilizationPercentage` | Target CPU utilization percentage      | `80`        |

### Observability

This Helm chart includes several features to enhance the observability of the Preloop application. These features are disabled by default and can be enabled and configured through the `values.yaml` file.

#### Performance Profiling

Performance profiling is provided by `pyinstrument`. When enabled, it profiles all API requests and saves the reports to a persistent volume.

To enable profiling, set the following values in your `values.yaml` file:

```yaml
profiling:
  enabled: true
  storage:
    pvc:
      create: true
      size: 1Gi
```

#### Error Tracking with Sentry

Error tracking is provided by the Sentry SDK. When enabled, it captures and reports all unhandled exceptions to your Sentry project.

To enable Sentry, set the following values in your `values.yaml` file:

```yaml
sentry:
  enabled: true
  dsn: "YOUR_SENTRY_DSN"
```

#### OTLP export

Gateway completions and MCP tool calls can be exported as OpenTelemetry
traces (OTLP). Disabled by default. Copy-paste collector, Langfuse, and
Datadog configs live in `docs/guide/observability-otlp.md`.

```yaml
otlp:
  enabled: true
  endpoint: "http://otel-collector:4318"
  protocol: http/protobuf
  headersSecret:
    name: preloop-otlp-headers
    key: otlp-headers
  resource:
    serviceName: preloop
    deploymentEnvironment: production
```

#### Web Analytics

Privacy-friendly web analytics (Plausible-compatible) can be enabled per
environment without rebuilding the console image: the console nginx injects
the tracking snippet into every HTML page at serve time.

```yaml
analytics:
  enabled: true
  domain: "example.com"                                # site id (data-domain)
  scriptUrl: "https://plausible.example.com/js/script.js"
```

The frontend fires conversion events (`Signup`, `Signup Click`, `Demo Click`,
`Install Copy`) through `window.plausible`; register them as custom-event
goals in your analytics platform to measure conversions. For non-Plausible
platforms, set `analytics.customSnippet` to a raw HTML snippet instead
(injected before `</head>`; must not contain single quotes).

## Configuration

### PostgreSQL with PGVector

This chart deploys PostgreSQL with the PGVector extension by default, which is required for vector search capabilities in Preloop. If you prefer to use an external PostgreSQL instance, ensure that it has the PGVector extension installed.

### Using an external PostgreSQL database

To use an external PostgreSQL database, set `database.external=true` and
prefer `database.urlFromSecret` so the URL (password and sslmode) is not
stored in values committed to git. See [Private cluster](#private-cluster).

```yaml
database:
  enabled: true
  external: true
  urlFromSecret:
    name: preloop-db
    key: database-url
  externalDatabase:
    host: postgres.internal
    port: 5432
    database: preloop
    sslMode: verify-full
```

### JWT Authentication

Preloop uses JWT for authentication. By default, it uses a placeholder JWT secret. For production deployments, you should set a proper JWT secret:

```yaml
environment:
  jwtSecret: your-secure-jwt-secret
```

### Ingress Configuration

To enable ingress, set `ingress.enabled=true` and configure the ingress hosts.
The chart renders two Ingress objects on that host: one sending `/` to the
console Service, and one sending `/openai`, `/anthropic`, and `/gemini` to
the gateway Service so streaming model traffic does not hairpin through
console nginx.

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: preloop.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: preloop-tls
      hosts:
        - preloop.example.com
```

### Scaling the deployment

For production environments containing concurrent agent executions or human-in-the-loop workflows, you must enable autoscaling:

```yaml
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 80
```

**Note:** This dynamically spans a `HorizontalPodAutoscaler` across both the Preloop REST API (`deployment-api.yaml`) and all asynchronous Worker pods (`spacesync-worker-deployment.yaml`).
If you need to statically define individual worker limits, you can adjust `worker.pools[].replicaCount` directly in `values.yaml`.

#### High Availability & Capacity Planning
When utilizing the Model Gateway to stream heavy upstream AI contexts (like giant PR Reviews or complex agent interactions), ensure corresponding capacity:
1. **CPU / Memory**: The default requests are `500m / 512Mi`. Ensure your node has sufficient headroom.
2. **Database Connections**: If `autoscaling.maxReplicas` is extremely high, adjust the `max_connections` parameter in the database template (default is 200). You may need to run `PgBouncer` to pool connections efficiently.

## Health Endpoints

The API server exposes the following health endpoints:

| Endpoint | Description |
|----------|-------------|
| `/api/v1/health` | Full health check including database connectivity |
| `/api/v1/ping` | Lightweight liveness probe (no database check) |

For Kubernetes probes, `/api/v1/ping` is recommended for liveness checks (fast, no dependencies) while `/api/v1/health` is suitable for readiness checks.

### In-cluster health monitor

When `healthMonitor.enabled` is `true` (default), the chart deploys a lightweight pod that polls the internal API health endpoint every 60 seconds. This avoids false negatives from external uptime checks over unreliable network paths. Configure via:

```yaml
healthMonitor:
  enabled: true
  url: ""  # defaults to http://<release>-api:80/api/v1/health
  intervalSeconds: 60
  timeoutSeconds: 10
  failuresBeforeAlert: 3
```

Logs emit `OK`, `UNHEALTHY`, or `ALERT` lines suitable for log-based alerting.

## Database Production Settings

For production deployments, enable resilience, logging, and query analysis:

```yaml
database:
  cnpg:
    instances: 3  # Primary + 2 standbys for HA
    storage:
      size: 20Gi  # Adequate storage for production data
    resilience:
      enabled: true
      statement_timeout: "300000"  # 5 minutes
      idle_in_transaction_session_timeout: "300000"
      lock_timeout: "60000"
    logging:
      enabled: true
      log_min_duration_statement: "1000"  # Log queries > 1 second
    queryAnalysis:
      enabled: true  # Enables pg_stat_statements and auto_explain
      autoExplainMinDuration: "1000"
```

### Query Performance Analysis

When `queryAnalysis.enabled` is true, you can find slow queries with:

```sql
-- Top 10 slowest queries by total time
SELECT query, calls, total_exec_time, mean_exec_time, rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;

-- Reset statistics after optimization
SELECT pg_stat_statements_reset();
```

### Continuous Backups (CNPG WAL archiving + scheduled base backups)

The chart can configure CloudNativePG continuous backups to any S3-compatible
object store. Enabling `database.cnpg.backup.enabled`:

- turns on **continuous WAL archiving** on the Cluster (recovery point is
  typically under 5 minutes — no need for deploy-time `pg_dump` backups), and
- creates a **`ScheduledBackup`** CR for periodic base backups (bounds WAL
  replay time on restore and anchors the retention policy).

**1. Create the credentials secret** (once per namespace):

```bash
kubectl create secret generic preloop-db-backup-s3 -n <namespace> \
  --from-literal=ACCESS_KEY_ID=<access-key> \
  --from-literal=SECRET_ACCESS_KEY=<secret-key>
```

**2. Enable via values** (see `values-backup-prod.yaml` /
`values-backup-staging.yaml` for complete profiles):

```yaml
database:
  cnpg:
    backup:
      enabled: true
      destinationPath: "s3://my-bucket/cnpg/my-cluster"  # required
      endpointURL: ""       # set for MinIO/R2/Ceph; empty for AWS S3
      retentionPolicy: "30d"
      s3Credentials:
        secretName: "preloop-db-backup-s3"
      scheduled:
        enabled: true
        schedule: "0 0 2 * * *"  # CNPG cron: SIX fields, seconds first
        immediate: true          # take a base backup right away
        # none: base backups survive ScheduledBackup deletion (helm
        # upgrade with scheduled.enabled=false, helm uninstall). self
        # would garbage-collect every backup the schedule created.
        backupOwnerReference: none
```

Notes:

- The CNPG cron `schedule` has **six** fields (`seconds minutes hours
  day-of-month month day-of-week`), unlike standard Kubernetes CronJobs.
- Enabling backups on a running cluster is a configuration reload, not a
  restart (CNPG always runs with `archive_mode=on`).
- Each cluster must own a **unique** `destinationPath`+`serverName`
  combination. Never point two live clusters at the same path.
- `backupOwnerReference` defaults to `none` so base backups are not
  garbage-collected when the `ScheduledBackup` is removed (a helm
  upgrade that sets `scheduled.enabled=false`, or `helm uninstall`).
  Set `self` only if you want that cascade.

**Verify** after enabling:

```bash
# WAL archiving healthy? (continuousArchiving condition should be True)
kubectl -n <ns> get cluster <cluster-name> \
  -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")]}'

# First base backup completed?
kubectl -n <ns> get backups
kubectl -n <ns> get scheduledbackup
```

You can also trigger an on-demand base backup at any time (e.g. before a
risky migration) without touching the deploy pipeline:

```bash
kubectl -n <ns> create -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  generateName: preloop-db-ondemand-
spec:
  cluster:
    name: <cluster-name>  # e.g. preloop-db
EOF
```

#### Restore procedure

Restores bootstrap a **new** Cluster from the object store (optionally to a
point in time), they do not restore in place:

1. Create a recovery manifest (adjust names, storage and credentials to match
   the environment; the `externalClusters.serverName` must match the name the
   backups were written under — by default the old cluster's name):

   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata:
     name: preloop-db-restore
   spec:
     instances: 1
     storage:
       size: 10Gi  # >= original
     superuserSecret:
       name: preloop-db-superuser
     enableSuperuserAccess: true
     bootstrap:
       recovery:
         source: origin
         # Optional point-in-time recovery:
         # recoveryTarget:
         #   targetTime: "2026-08-19 10:00:00+00"
     externalClusters:
       - name: origin
         barmanObjectStore:
           destinationPath: "s3://my-bucket/cnpg/my-cluster"
           # endpointURL: "https://..."  # if S3-compatible
           serverName: preloop-db  # folder the backups were written under
           s3Credentials:
             accessKeyId:
               name: preloop-db-backup-s3
               key: ACCESS_KEY_ID
             secretAccessKey:
               name: preloop-db-backup-s3
               key: SECRET_ACCESS_KEY
           wal:
             compression: gzip
   ```

2. `kubectl apply -n <ns> -f restore.yaml` and wait for the cluster to become
   `Cluster in healthy state` (`kubectl -n <ns> get cluster preloop-db-restore -w`).
3. Validate the data (connect via `kubectl -n <ns> exec -it preloop-db-restore-1 -- psql`).
4. Cut over: either repoint the application `database.host` at the restored
   cluster's `-rw` service, or (cleaner) redeploy the chart with
   `database.cnpg.name=preloop-db-restore` **and** a fresh
   `backup.serverName`/`destinationPath` so the restored cluster archives to
   a new location instead of overwriting the archive it recovered from.
5. Decommission the old cluster only after the new one is verified.

## Upgrading the Chart

### To 1.0.0

No special actions are required when upgrading from previous versions.

### Native conversation checkpoints

`values-native-checkpoints.yaml` is an optional overlay using the existing shared
`extraEnv` support. Chart defaults remain disabled. Deploy the transaction lock
corrections before enabling uploads, preserve existing signing/encryption keys,
and merge the overlay entries into your full `extraEnv` list. The overlay's
16 MiB compressed upload cap fits below the default 32 MiB proxy limit. See the
[deployment prerequisites](../../docs/guide/flows/durable-implementation-feedback.md#deployment-prerequisites)
for retention, quota, egress, rollback and validation requirements.
