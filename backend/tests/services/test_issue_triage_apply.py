"""Behavioral issue triage tests with local, deterministic provider state."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from preloop.schemas.issue_triage import IssueTriageApply, TriageIssue
from preloop.services.issue_triage import (
    END,
    START,
    STANDARD,
    apply_triage,
    complexity_scheme,
    get_context,
    merge_assessment,
)


class FakeProvider:
    def __init__(self, names: list[str] | None = None) -> None:
        self.issue = TriageIssue(
            title="Validate names",
            body="Keep original requirements.\n",
            url="https://github.com/example/widgets/issues/1",
            state="open",
            labels=["bug", "P1"],
            updated_at="2026-01-01T00:00:00Z",
        )
        self.rows = [{"name": n, "description": ""} for n in (names or [])]
        self.operations: list[Any] = []
        self.fail_label: str | None = None
        self.fail_delta = False
        self.after_body_labels: list[str] = []
        self.receipts: list[dict[str, Any]] = []

    async def read_issue(self) -> TriageIssue:
        return self.issue.model_copy(deep=True)

    async def catalogue(self) -> list[dict[str, str]]:
        return deepcopy(self.rows)

    async def create_label(self, name: str) -> None:
        if name == self.fail_label:
            raise ValueError("label_create_failed")
        self.rows.append(
            {
                "name": name,
                "description": "Preloop issue complexity: " + name.split(":")[-1],
            }
        )
        self.operations.append(("create", name))

    async def write_content(self, title: str, body: str) -> None:
        target = self.issue.model_copy(update={"title": title, "body": body})
        assert target.revision in self.receipts[-1]["expected_revisions"]
        self.issue = target
        self.issue.labels.extend(self.after_body_labels)
        self.operations.append(("content", title, body))

    async def update_labels(self, add: list[str], remove: list[str]) -> None:
        # Record/replay GitHub's separate add then removals as actual states.
        if add:
            self.issue.labels = sorted(set(self.issue.labels) | set(add))
            assert self.issue.revision in self.receipts[-1]["expected_revisions"]
        if self.fail_delta:
            raise ValueError("label_remove_failed")
        for label in remove:
            self.issue.labels.remove(label)
            assert self.issue.revision in self.receipts[-1]["expected_revisions"]
        self.operations.append(("delta", add, remove))

    def record(self, receipt: dict[str, Any]) -> None:
        self.receipts.append(deepcopy(receipt))


async def request_for(
    provider: FakeProvider, label: str | None = "complexity:low"
) -> IssueTriageApply:
    context = await get_context(provider)
    return IssueTriageApply(
        expected_revision=context.expected_revision,
        complexity_label=label,
        assessment="Remaining behavior: reject an empty name.\nAcceptance: POST with an empty name returns 400.\nComplexity: low, localized validation with existing tests.",
    )


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["size:S", "size:M", "size:L", "P1"], ["size:L", "size:M", "size:S"]),
        (["XS", "S", "M", "L", "XL"], ["L", "M", "S", "XL", "XS"]),
        (["effort::simple", "effort::complex"], ["effort::complex", "effort::simple"]),
        (["easy", "medium", "hard"], ["easy", "hard", "medium"]),
    ],
)
def test_existing_vocabulary_is_reused(labels: list[str], expected: list[str]) -> None:
    scheme = complexity_scheme([{"name": n, "description": ""} for n in labels])
    assert scheme.labels == expected
    assert not scheme.create_missing


@pytest.mark.parametrize(
    "labels",
    [
        ["low", "medium", "high"],
        ["size:S", "complexity:low"],
        ["size:S", "S", "M", "L"],
        ["S", "M", "L", "easy", "medium", "hard"],
    ],
)
def test_ambiguous_vocabulary_is_not_invented_or_overwritten(labels: list[str]) -> None:
    with pytest.raises(ValueError):
        complexity_scheme([{"name": n, "description": ""} for n in labels])


def test_explicit_standalone_complexity_descriptions_disambiguate_priority() -> None:
    rows = [
        {"name": n, "description": "Implementation complexity"}
        for n in ["low", "medium", "high"]
    ]
    assert complexity_scheme(rows).labels == ["high", "low", "medium"]


@pytest.mark.parametrize("body", [START, END, END + START, START + END + START + END])
def test_ambiguous_marker_content_never_deletes_human_text(body: str) -> None:
    with pytest.raises(ValueError):
        merge_assessment(body, "New acceptance")


def test_managed_body_preserves_outside_text_exactly() -> None:
    original = "Human context  \n\n" + START + "\nold\n" + END + "\nHuman addendum  "
    new = merge_assessment(original, "Updated acceptance")
    assert new.startswith("Human context  \n\n")
    assert new.endswith("\nHuman addendum  ")
    assert merge_assessment(new, "Updated acceptance") == new


@pytest.mark.asyncio
async def test_default_scheme_updates_issue_itself_and_preserves_unrelated_labels() -> (
    None
):
    provider = FakeProvider()
    result = await apply_triage(provider, await request_for(provider), provider.record)
    assert result.status == "updated"
    assert [row["name"] for row in provider.rows] == STANDARD
    assert set(provider.issue.labels) == {"bug", "P1", "complexity:low"}
    assert "POST with an empty name returns 400" in provider.issue.body
    assert provider.issue.body.startswith("Keep original requirements.\n")
    assert provider.receipts[-1]["provider_revision"] == provider.issue.revision
    assert provider.receipts[-1]["provider_updated_at"] == provider.issue.updated_at


@pytest.mark.asyncio
async def test_existing_scheme_changes_only_its_labels() -> None:
    provider = FakeProvider(["size:S", "size:L"])
    provider.issue.labels += ["size:L", "release:next"]
    result = await apply_triage(
        provider, await request_for(provider, "size:S"), provider.record
    )
    assert result.status == "updated"
    assert set(provider.issue.labels) == {"bug", "P1", "release:next", "size:S"}
    assert not any(op[0] == "create" for op in provider.operations)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["body", "title", "state", "complexity"])
async def test_stale_input_has_no_external_effects(change: str) -> None:
    provider = FakeProvider(STANDARD)
    request = await request_for(provider)
    if change == "complexity":
        provider.issue.labels.append("complexity:high")
    else:
        setattr(provider.issue, change, "closed" if change == "state" else "Human edit")
    result = await apply_triage(provider, request, provider.record)
    assert result.status == "conflict"
    assert provider.operations == []
    assert provider.receipts == []


@pytest.mark.asyncio
async def test_unrelated_label_change_is_preserved_without_stale_failure() -> None:
    provider = FakeProvider(STANDARD)
    request = await request_for(provider)
    provider.issue.labels.append("human-added")
    result = await apply_triage(provider, request, provider.record)
    assert result.status == "updated"
    assert "human-added" in provider.issue.labels


@pytest.mark.asyncio
async def test_partial_default_creation_can_complete_on_retry() -> None:
    provider = FakeProvider()
    provider.fail_label = "complexity:high"
    result = await apply_triage(provider, await request_for(provider), provider.record)
    assert result.status == "partial"
    assert [row["name"] for row in provider.rows] == STANDARD[:2]
    assert START not in provider.issue.body
    provider.fail_label = None
    result = await apply_triage(
        provider, await request_for(provider, "complexity:high"), provider.record
    )
    assert result.status == "updated"
    assert [row["name"] for row in provider.rows] == STANDARD


@pytest.mark.asyncio
async def test_custom_partial_family_is_never_augmented() -> None:
    provider = FakeProvider(["complexity:low"])
    result = await apply_triage(
        provider, await request_for(provider, "complexity:high"), provider.record
    )
    assert result.status == "failed"
    assert provider.operations == []


@pytest.mark.asyncio
async def test_context_change_introducing_scheme_prevents_standard_creation() -> None:
    provider = FakeProvider()
    request = await request_for(provider)
    provider.rows = [{"name": "size:S", "description": ""}]
    result = await apply_triage(provider, request, provider.record)
    assert result.status == "conflict"
    assert provider.operations == []


@pytest.mark.asyncio
async def test_repeating_same_assessment_is_noop_with_receipt() -> None:
    provider = FakeProvider(STANDARD)
    await apply_triage(provider, await request_for(provider), provider.record)
    provider.operations.clear()
    result = await apply_triage(provider, await request_for(provider), provider.record)
    assert result.status == "unchanged"
    assert provider.operations == []
    assert provider.receipts[-1]["provider_revision"] == provider.issue.revision


@pytest.mark.asyncio
async def test_label_partial_failure_reports_actual_observed_issue() -> None:
    provider = FakeProvider(STANDARD)
    provider.issue.labels.append("complexity:high")
    provider.fail_delta = True
    result = await apply_triage(provider, await request_for(provider), provider.record)
    assert result.status == "partial"
    assert result.issue is not None
    assert "complexity:low" in result.issue.labels
    assert "complexity:high" in result.issue.labels
    assert result.operations[-1]["state"] == "requested"
    assert START in result.issue.body


@pytest.mark.asyncio
async def test_human_complexity_change_after_body_is_not_removed() -> None:
    provider = FakeProvider(STANDARD)
    provider.after_body_labels = ["complexity:high"]
    result = await apply_triage(provider, await request_for(provider), provider.record)
    assert result.status == "partial"
    assert "complexity:high" in provider.issue.labels
    assert not any(op[0] == "delta" for op in provider.operations)


@pytest.mark.asyncio
async def test_unrelated_label_race_preserves_early_and_label_intents() -> None:
    labels = [f"size:{n}" for n in range(7)]
    provider = FakeProvider(labels)
    provider.issue.labels.extend(labels[1:])
    provider.after_body_labels = ["human-added"]
    result = await apply_triage(
        provider, await request_for(provider, labels[0]), provider.record
    )
    assert result.status == "updated"
    assert "human-added" in provider.issue.labels
    assert len(provider.receipts[-1]["expected_revisions"]) == 8
    assert (
        provider.receipts[0]["expected_revisions"][0]
        in provider.receipts[-1]["expected_revisions"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_receipt_persistence_failure_has_honest_partial_state(
    after_write: bool,
) -> None:
    provider = FakeProvider(STANDARD)

    def record(receipt: dict[str, Any]) -> None:
        if not after_write or "provider_revision" in receipt:
            raise SQLAlchemyError("Unavailable database")
        provider.record(receipt)

    result = await apply_triage(provider, await request_for(provider), record)
    assert result.status == ("partial" if after_write else "failed")
    assert result.reason == "triage_receipt_failed"
    assert bool(provider.operations) == after_write


@pytest.mark.asyncio
async def test_unestimated_complexity_updates_issue_without_fabricating_label() -> None:
    provider = FakeProvider()
    result = await apply_triage(
        provider, await request_for(provider, None), provider.record
    )
    assert result.status == "partial"
    assert result.reason == "complexity_not_estimated"
    assert START in provider.issue.body
    assert provider.rows == []
    assert provider.issue.labels == ["bug", "P1"]


def test_apply_schema_rejects_unrelated_authority_fields() -> None:
    with pytest.raises(ValidationError):
        IssueTriageApply(
            expected_revision="a" * 64, assessment="Fix issue", model="other"
        )


def test_gitlab_atomic_delta_intent_has_no_speculative_intermediate_states() -> None:
    from preloop.services.issue_triage import _intent

    issue = FakeProvider().issue
    issue.labels += ["complexity:high", "complexity:medium"]
    receipt = _intent(
        issue,
        issue.title,
        issue.body,
        ["complexity:low"],
        ["complexity:high", "complexity:medium"],
        atomic_labels=True,
    )
    final = issue.model_copy(update={"labels": ["bug", "P1", "complexity:low"]})
    assert receipt["expected_revisions"] == [final.revision]
