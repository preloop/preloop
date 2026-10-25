"""Optional durable chat worker uses the same runtime identity as the API."""

from __future__ import annotations

import yaml

from tests.helm.chart_helpers import helm_template, helm_template_all


def test_chat_worker_disabled_by_default() -> None:
    docs = list(yaml.safe_load_all(helm_template_all()))
    assert not any(
        doc and doc.get("metadata", {}).get("name", "").endswith("-chat-worker")
        for doc in docs
    )


def test_chat_worker_runtime_and_operator_configuration(tmp_path) -> None:
    pod_settings = {
        "podLabels": {"example.com/workload-identity": "chat"},
        "podAnnotations": {"example.com/annotation": "chat"},
        "nodeSelector": {"kubernetes.io/os": "linux"},
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "example.com/pool",
                                    "operator": "In",
                                    "values": ["workers"],
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "tolerations": [
            {
                "key": "example.com/workload",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ],
    }
    values = tmp_path / "chat-values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                **pod_settings,
                "chatWorker": {
                    "enabled": True,
                    "replicaCount": 2,
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
                },
                "image": {"repository": "example.com/preloop", "tag": "test"},
                "existingSecret": "shared-credentials",
                "security": {"encryptionKey": "synthetic-key"},
                "database": {"urlFromSecret": {"name": "shared-db", "key": "url"}},
                "nats": {"url": "nats://bus.example.com:4222"},
                "extraEnv": [
                    {"name": "SSL_CERT_FILE", "value": "/trust/ca.pem"},
                    {"name": "MODEL_ENDPOINT_ALLOWED_CIDRS", "value": "10.0.0.0/24"},
                ],
                "extraEnvFrom": [{"secretRef": {"name": "operator-settings"}}],
                "extraVolumes": [
                    {"name": "trust", "secret": {"secretName": "provider-ca"}}
                ],
                "extraVolumeMounts": [
                    {"name": "trust", "mountPath": "/trust", "readOnly": True}
                ],
                "agentExecution": {"kubeconfigSecret": "execution-credentials"},
            }
        )
    )
    doc = yaml.safe_load(
        helm_template(
            "templates/chat-worker-deployment.yaml", values_files=[str(values)]
        )
    )
    assert doc["kind"] == "Deployment"
    assert doc["spec"]["replicas"] == 2
    pod = doc["spec"]["template"]["spec"]
    for key in ("nodeSelector", "affinity", "tolerations"):
        assert pod[key] == pod_settings[key]
    assert (
        doc["spec"]["template"]["metadata"]["labels"]["example.com/workload-identity"]
        == "chat"
    )
    assert (
        doc["spec"]["template"]["metadata"]["annotations"]["example.com/annotation"]
        == "chat"
    )
    container = pod["containers"][0]
    assert container["image"] == "example.com/preloop:test"
    assert container["command"] == ["python", "-m", "preloop.services.chat_worker"]
    assert "livenessProbe" not in container
    assert "readinessProbe" not in container
    env = {item["name"]: item for item in container["env"]}
    assert env["PRELOOP_SERVICE_ROLE"]["value"] == "chat"
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "shared-db",
        "key": "url",
    }
    for name, key in [
        ("SECRET_KEY", "jwt-secret"),
        ("SECURITY__ENCRYPTION_KEY", "encryption-key"),
    ]:
        assert env[name]["valueFrom"]["secretKeyRef"] == {
            "name": "shared-credentials",
            "key": key,
        }
    assert env["NATS_URL"]["value"] == "nats://bus.example.com:4222"
    assert env["DATABASE_POOL_SIZE"]["value"] == "2"
    assert env["DATABASE_MAX_OVERFLOW"]["value"] == "4"
    assert env["SSL_CERT_FILE"]["value"] == "/trust/ca.pem"
    assert env["MODEL_ENDPOINT_ALLOWED_CIDRS"]["value"] == "10.0.0.0/24"
    assert container["envFrom"] == [{"secretRef": {"name": "operator-settings"}}]
    assert {"name": "trust", "mountPath": "/trust", "readOnly": True} in container[
        "volumeMounts"
    ]
    assert {"name": "trust", "secret": {"secretName": "provider-ca"}} in pod["volumes"]
    assert (
        env["PRELOOP_EXECUTION_KUBECONFIG"]["value"]
        == "/var/run/preloop/execution/kubeconfig"
    )
    assert any(
        mount["name"] == "execution-kubeconfig" for mount in container["volumeMounts"]
    )
    assert container["resources"]["requests"]["cpu"] == "100m"


def test_chat_worker_default_runtime_matches_api_secrets() -> None:
    overrides = ["chatWorker.enabled=true"]
    worker = yaml.safe_load(
        helm_template("templates/chat-worker-deployment.yaml", overrides)
    )
    api = yaml.safe_load(helm_template("templates/api-deployment.yaml", overrides))
    worker_pod = worker["spec"]["template"]
    api_pod = api["spec"]["template"]
    worker_container = worker_pod["spec"]["containers"][0]
    worker_env = {item["name"]: item for item in worker_container["env"]}
    api_env = {item["name"]: item for item in api_pod["spec"]["containers"][0]["env"]}
    for name in [
        "DATABASE_URL",
        "SECRET_KEY",
        "NATS_URL",
        "PRELOOP_URL",
        "JWT_ALGORITHM",
    ]:
        assert worker_env[name] == api_env[name]
    assert worker_container["image"] == api_pod["spec"]["containers"][0]["image"]
    assert (
        worker_pod["spec"]["serviceAccountName"]
        == api_pod["spec"]["serviceAccountName"]
    )
    assert (
        worker_pod["metadata"]["annotations"]["checksum/credentials"]
        == api_pod["metadata"]["annotations"]["checksum/credentials"]
    )
    assert "SECURITY__ENCRYPTION_KEY" not in worker_env
    assert "volumeMounts" not in worker_container
    assert "volumes" not in worker_pod["spec"]
