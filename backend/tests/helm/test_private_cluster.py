"""Guards for private-cluster Helm values: extra CA, secrets, ClusterIP."""

from __future__ import annotations

from typing import Dict, List

import yaml

from tests.helm.chart_helpers import (
    CHART_DIR,
    helm_template,
    helm_template_all,
    load_values,
)

PRIVATE_VALUES = "values-private-cluster.yaml"


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _deployments(rendered: str) -> List[Dict]:
    return [doc for doc in _docs(rendered) if doc.get("kind") == "Deployment"]


def _container_env(deployment: Dict) -> Dict[str, Dict]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = container.get("env") or []
    return {item["name"]: item for item in env}


def test_private_overlay_leaves_otlp_disabled() -> None:
    """Private-cluster overlay must not turn on OTLP; defaults stay off."""
    overlay = yaml.safe_load((CHART_DIR / PRIVATE_VALUES).read_text())
    otlp = overlay.get("otlp") or {}
    assert otlp.get("enabled", False) is False
    assert load_values()["otlp"]["enabled"] is False


def test_default_extra_env_and_volumes_are_empty() -> None:
    values = load_values()
    assert values["extraEnv"] == []
    assert values["extraVolumes"] == []
    assert values["extraVolumeMounts"] == []
    assert values["existingSecret"] == ""
    assert values["database"]["urlFromSecret"]["name"] == ""


def test_private_overlay_is_clusterip_with_ingress() -> None:
    overlay = yaml.safe_load((CHART_DIR / PRIVATE_VALUES).read_text())
    assert overlay["service"]["type"] == "ClusterIP"
    assert overlay["ingress"]["enabled"] is True
    assert overlay["ingress"]["hosts"][0]["host"] == "preloop.internal"
    assert overlay["imagePullSecrets"][0]["name"] == "registry-pull"
    assert overlay["existingSecret"] == "preloop-app"
    assert overlay["database"]["external"] is True
    assert overlay["database"]["urlFromSecret"]["name"] == "preloop-db"


def test_private_overlay_has_placeholder_secrets_only() -> None:
    """Example values must not inline real keys or passwords."""
    text = (CHART_DIR / PRIVATE_VALUES).read_text()
    forbidden = (
        "sk-",
        "BEGIN ",
        "b6fe6fa9-cf7c-4798-8328-f778712062df",
    )
    for needle in forbidden:
        assert needle not in text, f"example values contain {needle!r}"
    overlay = yaml.safe_load(text)
    assert overlay["environment"]["jwtSecret"] == ""
    assert overlay["database"]["externalDatabase"]["password"] == ""


def test_private_overlay_renders_extra_ca_on_gateway_and_api() -> None:
    for template, name_suffix in (
        ("templates/gateway-deployment.yaml", "gateway"),
        ("templates/api-deployment.yaml", "api"),
    ):
        rendered = helm_template(template, values_files=[PRIVATE_VALUES])
        docs = _docs(rendered)
        assert len(docs) == 1
        dep = docs[0]
        assert dep["metadata"]["name"].endswith(name_suffix)
        spec = dep["spec"]["template"]["spec"]
        env = _container_env(dep)
        assert env["SSL_CERT_FILE"]["value"] == "/etc/ssl/private-ca/ca.crt"
        assert env["REQUESTS_CA_BUNDLE"]["value"] == "/etc/ssl/private-ca/ca.crt"
        volume_names = {vol["name"] for vol in spec.get("volumes") or []}
        assert "private-ca" in volume_names
        mounts = {
            mount["name"]: mount
            for mount in spec["containers"][0].get("volumeMounts") or []
        }
        assert mounts["private-ca"]["mountPath"] == "/etc/ssl/private-ca"
        pull = spec.get("imagePullSecrets") or []
        assert pull[0]["name"] == "registry-pull"


def test_private_overlay_reads_database_url_from_secret() -> None:
    rendered = helm_template(
        "templates/gateway-deployment.yaml", values_files=[PRIVATE_VALUES]
    )
    env = _container_env(_docs(rendered)[0])
    db = env["DATABASE_URL"]
    assert "value" not in db
    assert db["valueFrom"]["secretKeyRef"]["name"] == "preloop-db"
    assert db["valueFrom"]["secretKeyRef"]["key"] == "database-url"


def test_private_overlay_uses_existing_secret_and_does_not_inline_jwt() -> None:
    rendered = helm_template_all(values_files=[PRIVATE_VALUES])
    docs = _docs(rendered)
    secrets = [doc for doc in docs if doc.get("kind") == "Secret"]
    chart_app_secret = [
        doc
        for doc in secrets
        if doc.get("metadata", {}).get("name") == "preloop"
        and "jwt-secret" in (doc.get("data") or {})
    ]
    assert chart_app_secret == []
    assert "b6fe6fa9-cf7c-4798-8328-f778712062df" not in rendered

    gateway = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment"
        and str(doc.get("metadata", {}).get("name", "")).endswith("-gateway")
    )
    env = _container_env(gateway)
    assert env["SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "preloop-app"


def test_sslmode_appended_when_building_external_database_url() -> None:
    rendered = helm_template(
        "templates/gateway-deployment.yaml",
        overrides=[
            "database.enabled=true",
            "database.external=true",
            "database.externalDatabase.host=postgres.internal",
            "database.externalDatabase.user=preloop",
            "database.externalDatabase.password=placeholder",
            "database.externalDatabase.database=preloop",
            "database.externalDatabase.sslMode=verify-full",
        ],
    )
    env = _container_env(_docs(rendered)[0])
    assert "sslmode=verify-full" in env["DATABASE_URL"]["value"]
    assert "postgres.internal" in env["DATABASE_URL"]["value"]


def test_default_render_does_not_inject_extra_ca() -> None:
    rendered = helm_template("templates/gateway-deployment.yaml")
    env = _container_env(_docs(rendered)[0])
    assert "SSL_CERT_FILE" not in env
    spec = _docs(rendered)[0]["spec"]["template"]["spec"]
    volume_names = {vol["name"] for vol in spec.get("volumes") or [] if vol}
    assert "private-ca" not in volume_names
