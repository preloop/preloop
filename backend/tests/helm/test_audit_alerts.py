"""Audit alerts cover durable backlog and the remaining best-effort path."""

import yaml

from tests.helm.chart_helpers import helm_template, load_values


def test_audit_alerts_are_opt_in() -> None:
    assert load_values()["auditAlerts"]["enabled"] is False


def test_alerts_cover_lag_failures_parked_and_drops() -> None:
    doc = yaml.safe_load(
        helm_template("templates/audit-alerts.yaml", ["auditAlerts.enabled=true"])
    )
    rules = doc["spec"]["groups"][0]["rules"]
    assert doc["kind"] == "PrometheusRule"
    assert {r["alert"] for r in rules} == {
        "PreloopAuditDrainLag",
        "PreloopAuditDeliveryFailures",
        "PreloopAuditParkedIntents",
        "PreloopAuditAdmissionDrops",
    }
    assert all('job="preloop"' in r["expr"] for r in rules)
    assert "max(" in rules[2]["expr"]  # Replicas observe the same database count.


def test_exporter_names_and_lag_threshold_are_configurable() -> None:
    doc = yaml.safe_load(
        helm_template(
            "templates/audit-alerts.yaml",
            [
                "auditAlerts.enabled=true",
                "auditAlerts.metrics.oldestPendingAge=custom_audit_age",
                "auditAlerts.lagSeconds=120",
                "auditAlerts.lagFor=10m",
                "auditAlerts.labels.release=monitoring",
            ],
        )
    )
    lag = doc["spec"]["groups"][0]["rules"][0]
    assert lag["expr"] == 'max(custom_audit_age{ job="preloop" }) > 120'
    assert lag["for"] == "10m"
    assert doc["metadata"]["labels"]["release"] == "monitoring"
