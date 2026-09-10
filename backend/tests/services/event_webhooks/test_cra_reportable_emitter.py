"""cra.reportable_vulnerability: one event per candidate, one clock each.

A 24 hour deadline that only exists inside an evidence pack nobody opened is
not a notification. These tests pin the routing, the payload and the
idempotency key, and they pin what the event is NOT: a filing. Preloop has
no connection to the ENISA single reporting platform.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from preloop.models.models.webhook_endpoint import WebhookDelivery
from preloop.services.event_webhooks.emitters import (
    emit_cra_reportable_vulnerabilities,
)
from preloop.services.event_webhooks.events import (
    EVENT_CRA_REPORTABLE_VULNERABILITY,
    EVENT_TYPE_DESCRIPTIONS,
    EVENT_TYPES_V1,
    deterministic_event_id,
)

DISCOVERED_AT = "2026-09-11T09:14:00Z"
DEADLINES = {
    "early_warning_24h": "2026-09-12T09:14:00Z",
    "notification_72h": "2026-09-14T09:14:00Z",
    "final_report_14d": "2026-09-25T09:14:00Z",
}


def _candidate(cve="CVE-2026-1234", *, reportable=True, affected=True):
    return {
        "id": cve,
        "actively_exploited": True,
        "exploited_evidence": "kev",
        "affected": {
            "value": affected,
            "source": "reachability",
            "detail": "call path reaches the vulnerable function",
        },
        "vex_status": None,
        "reportable": reportable,
        "discovered_at": DISCOVERED_AT,
        "deadlines": dict(DEADLINES),
        "status": "none",
    }


def _reporting(candidates):
    return {
        "assessment": "reportable_candidate",
        "basis": "KEV snapshot 2026-09-10; product affected",
        "kev_snapshot_date": "2026-09-10",
        "kev_source_url": "https://www.cisa.gov/kev.json",
        "candidates": candidates,
        "not_a_legal_determination": True,
    }


def _execution(result, execution_id=None):
    return SimpleNamespace(
        id=execution_id or uuid.uuid4(),
        result=result,
        end_time=None,
    )


@pytest.fixture
def flow(account):
    return SimpleNamespace(
        id=uuid.uuid4(), name="Release Security Audit", account_id=account.id
    )


def _deliveries(db_session, account_id):
    return (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == account_id)
        .all()
    )


def test_event_type_is_in_the_v1_catalogue():
    assert EVENT_CRA_REPORTABLE_VULNERABILITY in EVENT_TYPES_V1
    description = EVENT_TYPE_DESCRIPTIONS[EVENT_CRA_REPORTABLE_VULNERABILITY]
    assert "Article 14" in description
    assert "does not file" in description


def test_one_event_per_reportable_candidate(db_session, account, make_endpoint, flow):
    make_endpoint()
    execution = _execution(
        {
            "vuln_scan": {
                "reporting": _reporting([_candidate("CVE-1"), _candidate("CVE-2")])
            }
        }
    )

    emitted = emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    assert emitted == 2
    rows = _deliveries(db_session, account.id)
    assert len(rows) == 2
    cves = sorted(json.loads(json.dumps(row.payload))["data"]["cve"] for row in rows)
    assert cves == ["CVE-1", "CVE-2"]


def test_non_reportable_candidates_emit_nothing(
    db_session, account, make_endpoint, flow
):
    make_endpoint()
    execution = _execution(
        {
            "vuln_scan": {
                "reporting": _reporting(
                    [_candidate("CVE-1", reportable=False, affected=False)]
                )
            }
        }
    )

    assert emit_cra_reportable_vulnerabilities(db_session, execution, flow) == 0
    assert _deliveries(db_session, account.id) == []


def test_top_level_reporting_block_is_found(db_session, account, make_endpoint, flow):
    """Preset 005 writes the block at the root, not under vuln_scan."""
    make_endpoint()
    execution = _execution({"reporting": _reporting([_candidate()])})

    assert emit_cra_reportable_vulnerabilities(db_session, execution, flow) == 1


def test_result_without_a_reporting_block_emits_nothing(
    db_session, account, make_endpoint, flow
):
    make_endpoint()
    assert emit_cra_reportable_vulnerabilities(db_session, _execution(None), flow) == 0
    assert (
        emit_cra_reportable_vulnerabilities(db_session, _execution({"ok": True}), flow)
        == 0
    )
    assert _deliveries(db_session, account.id) == []


def test_repeat_emission_is_idempotent_per_execution_and_cve(
    db_session, account, make_endpoint, flow
):
    make_endpoint()
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)
    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    rows = _deliveries(db_session, account.id)
    assert len(rows) == 1
    expected = deterministic_event_id(
        f"{EVENT_CRA_REPORTABLE_VULNERABILITY}:{execution.id}:CVE-2026-1234"
    )
    assert rows[0].event_id == expected


def test_a_second_run_of_the_same_finding_is_a_separate_event(
    db_session, account, make_endpoint, flow
):
    """Idempotency is per execution: a re-audit is a new fact to deliver."""
    make_endpoint()
    first = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})
    second = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, first, flow)
    emit_cra_reportable_vulnerabilities(db_session, second, flow)

    assert len(_deliveries(db_session, account.id)) == 2


def test_payload_carries_the_clock_and_the_evidence(
    db_session, account, make_endpoint, flow
):
    make_endpoint()
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    data = _deliveries(db_session, account.id)[0].payload["data"]
    assert data["cve"] == "CVE-2026-1234"
    assert data["deadlines"] == DEADLINES
    assert data["discovered_at"] == DISCOVERED_AT
    assert data["exploited_evidence"] == "kev"
    assert data["affected"]["source"] == "reachability"
    assert data["assessment"] == "reportable_candidate"
    assert data["kev_snapshot_date"] == "2026-09-10"
    assert data["flow_name"] == "Release Security Audit"
    assert data["execution_id"] == str(execution.id)


def test_payload_says_it_is_not_a_filing(db_session, account, make_endpoint, flow):
    make_endpoint()
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    data = _deliveries(db_session, account.id)[0].payload["data"]
    assert data["not_a_legal_determination"] is True
    assert data["filing_is_manufacturer_responsibility"] is True


def test_envelope_occurred_at_is_awareness_not_delivery(
    db_session, account, make_endpoint, flow
):
    make_endpoint()
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    envelope = _deliveries(db_session, account.id)[0].payload
    assert envelope["occurred_at"].startswith("2026-09-11T09:14:00")
    assert envelope["type"] == EVENT_CRA_REPORTABLE_VULNERABILITY


def test_endpoint_filtered_to_other_types_receives_nothing(
    db_session, account, make_endpoint, flow
):
    make_endpoint(event_types=["flow.execution.finished"])
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    assert _deliveries(db_session, account.id) == []


def test_subscribing_to_the_type_receives_it(db_session, account, make_endpoint, flow):
    make_endpoint(event_types=[EVENT_CRA_REPORTABLE_VULNERABILITY])
    execution = _execution({"vuln_scan": {"reporting": _reporting([_candidate()])}})

    emit_cra_reportable_vulnerabilities(db_session, execution, flow)

    assert len(_deliveries(db_session, account.id)) == 1


def test_candidate_without_an_id_is_skipped(db_session, account, make_endpoint, flow):
    make_endpoint()
    candidate = _candidate()
    candidate["id"] = "   "
    execution = _execution({"vuln_scan": {"reporting": _reporting([candidate])}})

    assert emit_cra_reportable_vulnerabilities(db_session, execution, flow) == 0
