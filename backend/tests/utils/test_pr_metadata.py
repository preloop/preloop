"""Publisher-owned provenance parsing: round trip, dedup, malformed, oversize."""

from __future__ import annotations

import pytest

from preloop.utils.pr_metadata import (
    PROVENANCE_END,
    PROVENANCE_START,
    PublicationRecord,
    parse_provenance,
    provenance_block,
    upsert_provenance,
)

EXECUTION = "11111111-1111-4111-8111-111111111111"
REPAIR = "22222222-2222-4222-8222-222222222222"
HEAD = "a" * 40
REPAIR_HEAD = "b" * 40
PUBLIC_URL = "https://app.example.com"


def test_parse_provenance_canonicalizes_braced_execution_id() -> None:
    braced = "{11111111-1111-4111-8111-111111111111}"
    line = (
        f"- [Initial execution]({PUBLIC_URL}/console/flows/executions/{braced})"
        f" — published `{HEAD}`"
    )
    body = (
        PROVENANCE_START + "\n### Preloop executions\n\n" + line + "\n" + PROVENANCE_END
    )
    assert parse_provenance(body) == [PublicationRecord(EXECUTION, HEAD)]


def test_parse_provenance_round_trips_exact_records() -> None:
    records = [
        PublicationRecord(EXECUTION, HEAD),
        PublicationRecord(REPAIR, REPAIR_HEAD),
    ]
    block = provenance_block(records, PUBLIC_URL)
    assert parse_provenance(block) == records
    assert parse_provenance("Human body only\n") == []


def test_parse_provenance_after_upsert_appends_without_duplicate() -> None:
    existing = PublicationRecord(EXECUTION, HEAD)
    repair = PublicationRecord(REPAIR, REPAIR_HEAD)
    body = upsert_provenance("Human", [existing], PUBLIC_URL)
    body = upsert_provenance(body, [existing, repair, repair], PUBLIC_URL)
    parsed = parse_provenance(body)
    assert parsed == [existing, repair]
    assert body.count(PROVENANCE_START) == 1


@pytest.mark.parametrize(
    "body",
    [
        PROVENANCE_START,
        "Human\n" + PROVENANCE_END,
        PROVENANCE_START
        + "\n### Preloop executions\n\nnot a record\n"
        + PROVENANCE_END,
        PROVENANCE_START + "\n### Preloop executions\n" + PROVENANCE_END,
    ],
)
def test_parse_provenance_rejects_malformed_region(body: str) -> None:
    with pytest.raises(ValueError):
        parse_provenance(body)


def test_upsert_provenance_rejects_provider_oversize() -> None:
    with pytest.raises(ValueError, match="exceeds provider limit"):
        upsert_provenance(
            "x" * 70000,
            [PublicationRecord(EXECUTION, HEAD)],
            PUBLIC_URL,
        )
