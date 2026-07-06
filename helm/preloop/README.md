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

## Configuration

### PostgreSQL with PGVector

This chart deploys PostgreSQL with the PGVector extension by default, which is required for vector search capabilities in Preloop. If you prefer to use an external PostgreSQL instance, ensure that it has the PGVector extension installed.

### Using an external PostgreSQL database

To use an external PostgreSQL database, set `database.enabled=false` and configure the external database parameters:

```yaml
database:
  enabled: false
  external: true
  externalDatabase:
    host: my-postgresql.example.com
    port: 5432
    user: postgres
    password: my-password
    database: preloop
```

### JWT Authentication

Preloop uses JWT for authentication. By default, it uses a placeholder JWT secret. For production deployments, you should set a proper JWT secret:

```yaml
environment:
  jwtSecret: your-secure-jwt-secret
```

### Ingress Configuration

To enable ingress, set `ingress.enabled=true` and configure the ingress hosts:

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

## Upgrading the Chart

### To 1.0.0

No special actions are required when upgrading from previous versions.
