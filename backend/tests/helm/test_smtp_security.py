"""Chart users can select the SMTP transport security mode."""

import yaml

from tests.helm.chart_helpers import helm_template

TEMPLATES = [
    "templates/api-deployment.yaml",
    "templates/gateway-deployment.yaml",
    "templates/spacesync-worker-deployment.yaml",
    "templates/spacesync-scheduler-deployment.yaml",
    "templates/spacesync-monitor-deployment.yaml",
]


def _env(doc: dict) -> dict[str, object]:
    return {
        item["name"]: item.get("value")
        for container in doc["spec"]["template"]["spec"]["containers"]
        for item in container.get("env", [])
    }


def test_smtp_security_reaches_every_smtp_pod() -> None:
    """protects=ssl/none selectable via config.smtp.security;
    fails_when=a pod that gets SMTP_HOST lacks SMTP_SECURITY."""
    overrides = [
        "config.smtp.host=mail.example.com",
        "config.smtp.security=ssl",
    ]
    for template in TEMPLATES:
        docs = [d for d in yaml.safe_load_all(helm_template(template, overrides)) if d]
        for doc in docs:
            env = _env(doc)
            if "SMTP_HOST" in env:
                assert env.get("SMTP_SECURITY") == "ssl", template
