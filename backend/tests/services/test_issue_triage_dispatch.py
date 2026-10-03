"""Risk/readiness tags, the opt-in dispatch label and the spec section heading."""

from types import SimpleNamespace
from typing import Any

import pytest

from preloop.schemas.issue_triage import IssueTriageApply, TriageDispatch
from preloop.services.issue_triage import (
    SECTION_HEADING,
    STANDARD,
    STANDARD_READINESS,
    STANDARD_RISK,
    apply_triage,
    get_context,
    merge_assessment,
    readiness_scheme,
    risk_scheme,
)
from preloop.services.issue_triage_controller import dispatch_policy
from tests.services.test_issue_triage_apply import FakeProvider

ASSESSMENT = "### Problem\nSlow first render.\n### Acceptance criteria\n- [ ] p95 < 1s"


async def full_request(
    provider: FakeProvider,
    complexity: str | None = "complexity:low",
    risk: str | None = "risk:low",
    readiness: str | None = "readiness:ready",
) -> IssueTriageApply:
    context = await get_context(provider)
    return IssueTriageApply(
        expected_revision=context.expected_revision,
        complexity_label=complexity,
        risk_label=risk,
        readiness_label=readiness,
        assessment=ASSESSMENT,
    )


def test_section_heading_reads_as_a_spec() -> None:
    body = merge_assessment("One line issue.", "Plan")
    assert SECTION_HEADING == "## Ready for development"
    assert "\n## Ready for development\n\nPlan\n" in body
    assert "Implementation readiness" not in body


def test_risk_and_readiness_schemes_reuse_project_labels() -> None:
    rows = [
        {"name": n, "description": "project"}
        for n in ("risk:low", "risk:high", "readiness:ready", "bug")
    ]
    assert risk_scheme(rows).labels == ["risk:high", "risk:low"]
    assert not risk_scheme(rows).create_missing
    assert readiness_scheme(rows).labels == ["readiness:ready"]
    assert risk_scheme([]).labels == STANDARD_RISK
    assert readiness_scheme([]).create_missing


@pytest.mark.asyncio
async def test_receipt_applies_all_three_labels_and_the_dispatch_label() -> None:
    provider = FakeProvider()
    result = await apply_triage(
        provider,
        await full_request(provider),
        provider.record,
        dispatch_label="agent-ready",
    )
    assert result.status == "updated"
    assert set(provider.issue.labels) == {
        "bug",
        "P1",
        "complexity:low",
        "risk:low",
        "readiness:ready",
        "agent-ready",
    }
    created = {row["name"] for row in provider.rows}
    assert created == set(STANDARD) | set(STANDARD_RISK) | set(STANDARD_READINESS)
    dispatch = [
        op for op in result.operations if op["operation"] == "apply_dispatch_label"
    ]
    assert dispatch == [
        {
            "operation": "apply_dispatch_label",
            "label": "agent-ready",
            "state": "confirmed",
        }
    ]
    # One label delta: the dispatch label never lands before the assessed tags.
    assert [op for op in provider.operations if op[0] == "delta"] == [
        (
            "delta",
            ["complexity:low", "risk:low", "readiness:ready", "agent-ready"],
            [],
        )
    ]


@pytest.mark.asyncio
async def test_siblings_are_replaced_per_scheme_and_unrelated_labels_kept() -> None:
    names = STANDARD + STANDARD_RISK + STANDARD_READINESS
    provider = FakeProvider()
    provider.rows = [
        {
            "name": n,
            "description": f"Preloop issue {n.split(':')[0]}: {n.split(':')[1]}",
        }
        for n in names
    ]
    provider.issue.labels += ["complexity:high", "risk:high", "readiness:needs-spec"]
    result = await apply_triage(
        provider,
        await full_request(
            provider, "complexity:medium", "risk:medium", "readiness:blocked"
        ),
        provider.record,
    )
    assert result.status == "updated"
    assert set(provider.issue.labels) == {
        "bug",
        "P1",
        "complexity:medium",
        "risk:medium",
        "readiness:blocked",
    }
    assert not any(op[0] == "create" for op in provider.operations)


@pytest.mark.asyncio
async def test_label_outside_its_scheme_is_rejected_without_writes() -> None:
    provider = FakeProvider(STANDARD)
    result = await apply_triage(
        provider,
        await full_request(provider, risk="risk:catastrophic"),
        provider.record,
    )
    assert result.status == "failed"
    assert result.reason == "risk_label_not_in_current_scheme"
    assert provider.operations == []


@pytest.mark.asyncio
async def test_null_risk_and_readiness_keep_complexity_behaviour() -> None:
    provider = FakeProvider()
    result = await apply_triage(
        provider,
        await full_request(provider, risk=None, readiness=None),
        provider.record,
    )
    assert result.status == "updated"
    assert [row["name"] for row in provider.rows] == STANDARD
    assert set(provider.issue.labels) == {"bug", "P1", "complexity:low"}


@pytest.mark.asyncio
async def test_human_risk_change_during_apply_is_not_overwritten() -> None:
    provider = FakeProvider(STANDARD + STANDARD_RISK)
    provider.rows = [
        {
            "name": n,
            "description": f"Preloop issue {n.split(':')[0]}: {n.split(':')[1]}",
        }
        for n in STANDARD + STANDARD_RISK
    ]
    request = await full_request(provider, readiness=None)
    provider.after_body_labels = ["risk:high"]
    result = await apply_triage(provider, request, provider.record)
    assert result.status == "partial"
    assert "risk:high" in provider.issue.labels
    assert not any(op[0] == "delta" for op in provider.operations)


@pytest.mark.asyncio
async def test_no_dispatch_without_complexity_even_if_controller_asks() -> None:
    provider = FakeProvider()
    result = await apply_triage(
        provider,
        await full_request(provider, complexity=None),
        provider.record,
        dispatch_label="agent-ready",
    )
    assert result.status == "partial"
    assert "agent-ready" not in provider.issue.labels


@pytest.mark.asyncio
async def test_existing_dispatch_label_is_unchanged_not_reapplied() -> None:
    provider = FakeProvider()
    await apply_triage(
        provider, await full_request(provider), provider.record, dispatch_label="go"
    )
    provider.operations.clear()
    result = await apply_triage(
        provider, await full_request(provider), provider.record, dispatch_label="go"
    )
    assert result.status == "unchanged"
    assert provider.operations == []


def _request(**labels: Any) -> IssueTriageApply:
    values = {
        "complexity_label": "complexity:low",
        "risk_label": "risk:low",
        "readiness_label": "readiness:ready",
        **labels,
    }
    return IssueTriageApply(expected_revision="a" * 64, assessment="x", **values)


@pytest.mark.parametrize(
    "labels",
    [
        {"readiness_label": "readiness:needs-spec"},
        {"readiness_label": None},
        {"risk_label": "risk:medium"},
        {"risk_label": None},
        {"complexity_label": "complexity:high"},
        {"complexity_label": None},
    ],
)
def test_default_policy_withholds_dispatch_when_not_met(labels: dict) -> None:
    assert TriageDispatch(enabled=True).label_for(_request(**labels)) is None


def test_default_policy_dispatches_ready_low_risk_low_or_medium() -> None:
    policy = TriageDispatch(enabled=True)
    assert policy.label_for(_request()) == "agent-ready"
    assert policy.label_for(_request(complexity_label="complexity:medium")) == (
        "agent-ready"
    )
    assert TriageDispatch().label_for(_request()) is None


def test_policy_and_label_are_configurable() -> None:
    policy = TriageDispatch(
        enabled=True, label="implement", policy={"readiness": ["ready"]}
    )
    assert policy.label_for(_request(readiness_label="ready", risk_label=None)) == (
        "implement"
    )


def test_model_request_cannot_carry_a_dispatch_field() -> None:
    with pytest.raises(ValueError):
        IssueTriageApply(
            expected_revision="a" * 64, assessment="x", dispatch_label="agent-ready"
        )


@pytest.mark.parametrize(
    "config,expected",
    [
        (None, None),
        ({"sandbox_type": "exec"}, None),
        ({"dispatch": {"enabled": True}}, "agent-ready"),
        ({"agent_config": {"dispatch": {"enabled": True, "label": "go"}}}, "go"),
        ({"dispatch": {"enabled": True, "policy": {"color": ["x"]}}}, None),
        ({"dispatch": "yes"}, None),
    ],
)
def test_flow_agent_config_owns_the_dispatch_policy(
    config: Any, expected: str | None
) -> None:
    flow = SimpleNamespace(id="flow", agent_config=config)
    assert dispatch_policy(flow).label_for(_request()) == expected


def test_revision_is_unchanged_for_issues_without_risk_or_readiness_tags() -> None:
    from preloop.services.issue_triage import complexity_scheme, scope_revision

    issue = FakeProvider().issue
    rows = [{"name": n, "description": ""} for n in STANDARD]
    scheme = complexity_scheme(rows)
    legacy = scope_revision(issue, scheme)
    assert scope_revision(issue, scheme, risk_scheme(rows), readiness_scheme(rows)) == (
        legacy
    )
    tagged = issue.model_copy(update={"labels": [*issue.labels, "risk:high"]})
    assert scope_revision(tagged, scheme, risk_scheme(rows)) != legacy


@pytest.mark.asyncio
async def test_human_risk_edit_before_apply_is_a_conflict() -> None:
    provider = FakeProvider(STANDARD)
    provider.issue.labels.append("risk:medium")
    request = await full_request(provider, readiness=None)
    provider.issue.labels.remove("risk:medium")
    provider.issue.labels.append("risk:high")
    result = await apply_triage(provider, request, provider.record)
    assert result.status == "conflict"
    assert provider.operations == []


@pytest.mark.asyncio
async def test_too_many_stale_triage_labels_fail_before_any_write() -> None:
    provider = FakeProvider()
    names = STANDARD + STANDARD_RISK + STANDARD_READINESS
    provider.rows = [
        {
            "name": n,
            "description": f"Preloop issue {n.split(':')[0]}: {n.split(':')[1]}",
        }
        for n in names
    ]
    provider.issue.labels += [
        n for n in names if n not in {"complexity:low", "risk:low", "readiness:ready"}
    ]
    result = await apply_triage(provider, await full_request(provider), provider.record)
    assert result.status == "failed"
    assert result.reason == "too_many_conflicting_triage_labels"
    assert provider.operations == []
