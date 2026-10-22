"""Local kind infrastructure and a bounded post-renderer for the real chart."""

from __future__ import annotations

import sys
from typing import Any

import yaml


def infrastructure(
    name: str, env: dict[str, str], password: str
) -> list[dict[str, Any]]:
    """Only disposable namespace-scoped services; no operator or external DB."""
    objects: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "acceptance-defaults"},
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "defaultRequest": {"cpu": "100m", "memory": "512Mi"},
                        "default": {"cpu": "2", "memory": "2Gi"},
                    }
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "acceptance-credentials", "namespace": name},
            "type": "Opaque",
            "stringData": {
                **env,
                "POSTGRES_PASSWORD": password,
                "jwt-secret": env["SECRET_KEY"],
                "database-url": env["DATABASE_URL"],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "acceptance-bounds"},
            "spec": {
                "hard": {
                    "pods": "8",
                    "requests.cpu": "4",
                    "requests.memory": "6Gi",
                    "limits.cpu": "8",
                    "limits.memory": "12Gi",
                }
            },
        },
    ]
    for service in ("postgres", "nats"):
        port = 5432 if service == "postgres" else 4222
        objects.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": service},
                "spec": {
                    "selector": {"app": service},
                    "ports": [{"port": port, "targetPort": port}],
                },
            }
        )
        container: dict[str, Any] = {
            "name": service,
            "image": "pgvector/pgvector:pg16"
            if service == "postgres"
            else "nats:2.11-alpine",
            "imagePullPolicy": "Never",
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
            "ports": [{"containerPort": port}],
            "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 2},
        }
        pod: dict[str, Any] = {"containers": [container]}
        if service == "postgres":
            container["env"] = [
                {"name": "POSTGRES_DB", "value": "preloop"},
                {"name": "POSTGRES_USER", "value": "postgres"},
                {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                {
                    "name": "POSTGRES_PASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "acceptance-credentials",
                            "key": "POSTGRES_PASSWORD",
                        }
                    },
                },
            ]
            container["volumeMounts"] = [
                {"name": "data", "mountPath": "/var/lib/postgresql/data"}
            ]
            pod["volumes"] = [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": "postgres-data"},
                }
            ]
        else:
            container["args"] = ["-js", "-m", "8222"]
        workload = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": service},
            "spec": {
                "serviceName": service,
                "replicas": 1,
                "selector": {"matchLabels": {"app": service}},
                "template": {"metadata": {"labels": {"app": service}}, "spec": pod},
            },
        }
        objects.append(workload)
    objects.append(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "postgres-data"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "2Gi"}},
            },
        }
    )
    return objects


def values(image: str, env: dict[str, str]) -> dict[str, Any]:
    """Use chart interfaces for key continuity, hooks and fixture mounting."""
    repository, tag = image.rsplit(":", 1)
    return {
        "image": {"repository": repository, "tag": tag, "pullPolicy": "Never"},
        "existingSecret": "acceptance-credentials",
        "database": {
            "enabled": False,
            "external": True,
            "urlFromSecret": {"name": "acceptance-credentials", "key": "database-url"},
            "pool": {"api": {"size": 2, "maxOverflow": 1}},
        },
        "nats": {"enabled": False, "url": "nats://nats:4222"},
        "gateway": {"enabled": False},
        "healthMonitor": {"enabled": False},
        "worker": {"pools": []},
        "autoscaling": {"enabled": False},
        "api": {"replicaCount": 1, "terminationGracePeriodSeconds": 10},
        "environment": {
            "preloopUrl": "http://localhost:8000",
            "skipExecutionRecovery": True,
            "requireEmailVerification": False,
        },
        "extraEnv": [
            {
                "name": key,
                "valueFrom": {
                    "secretKeyRef": {"name": "acceptance-credentials", "key": key}
                },
            }
            for key in (
                "PRELOOP_DISABLE_TELEMETRY",
                "SECURITY__ENCRYPTION_KEY",
                "LITELLM_LOCAL_MODEL_COST_MAP",
                "MODEL_PRICE_LIVE_LOOKUP_ENABLED",
                "SECRET_KEY",
            )
        ],
        "extraVolumes": [
            {"name": "acceptance", "configMap": {"name": "acceptance-fixture"}}
        ],
        "extraVolumeMounts": [
            {"name": "acceptance", "mountPath": "/acceptance", "readOnly": True}
        ],
        "agentExecution": {
            "rbac": {"create": False},
            "networkPolicy": {"enabled": False},
        },
    }


def bounded_chart(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scale background/console workloads to zero and bound API/hook resources.

    The chart has no scheduler enable switch. Keep its objects in Helm's
    release manifest while ensuring this fixture cannot poll a tracker or
    execute a flow. Migration/init commands, images and env remain unmodified.
    """
    for document in documents:
        kind = document.get("kind")
        if kind not in ("Deployment", "Job"):
            continue
        if kind == "Deployment":
            component = (
                document["metadata"]
                .get("labels", {})
                .get("app.kubernetes.io/component")
            )
            document["spec"]["replicas"] = 1 if component == "api" else 0
        else:
            document["spec"]["backoffLimit"] = 0
            document["spec"]["activeDeadlineSeconds"] = 360
        pod = document["spec"]["template"]["spec"]
        for container in pod["containers"]:
            container["resources"] = {
                "requests": {"cpu": "100m", "memory": "512Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            }
    return documents


if __name__ == "__main__":
    docs = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
    yaml.safe_dump_all(bounded_chart(docs), sys.stdout)
