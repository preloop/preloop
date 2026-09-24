"""Owned provenance parsing and append for legacy continuation."""

from __future__ import annotations

from uuid import UUID

import pytest

from preloop.utils.pr_metadata import (
    PROVENANCE_END,
    PROVENANCE_START,
    PublicationRecord,
    append_provenance,
    parse_provenance,
    provenance_block,
    upsert_provenance,
)

EXECUTION = "11111111-1111-4111-8111-111111111111"
REPAIR = "22222222-2222-4222-8222-222222222222"
HEAD = "a" * 40
NEXT = "b" * 40
PUBLIC_URL = "https://app.example.com"


def test_parse_provenance_round_trip() -> None:
    records = [
        PublicationRecord(EXECUTION, HEAD),
        PublicationRecord(REPAIR, NEXT),
    ]
    body = "Human prefix\n" + provenance_block(records, PUBLIC_URL) + "\nHuman suffix"
    assert parse_provenance(body) == records
    assert append_provenance(body, records[0], PUBLIC_URL) == upsert_provenance(
        body, records, PUBLIC_URL
    )


def test_append_provenance_skips_duplicate_id_and_sha() -> None:
    first = PublicationRecord(EXECUTION, HEAD)
    body = upsert_provenance("Keep this prose.", [first], PUBLIC_URL)
    again = append_provenance(body, first, PUBLIC_URL)
    assert again == body
    assert again.count(EXECUTION) == 1
    extended = append_provenance(again, PublicationRecord(REPAIR, NEXT), PUBLIC_URL)
    assert parse_provenance(extended) == [first, PublicationRecord(REPAIR, NEXT)]
    assert extended.startswith("Keep this prose.")
    assert extended.count(PROVENANCE_START) == 1


def test_malformed_provenance_region_is_rejected() -> None:
    human = "Human prose stays.\n" + PROVENANCE_START + "\nnot a record\n"
    with pytest.raises(ValueError, match="Malformed"):
        parse_provenance(human)
    with pytest.raises(ValueError, match="Malformed"):
        append_provenance(human, PublicationRecord(EXECUTION, HEAD), PUBLIC_URL)
    broken = provenance_block([PublicationRecord(EXECUTION, HEAD)], PUBLIC_URL)
    broken = broken.replace("published", "published extra", 1)
    with pytest.raises(ValueError, match="Malformed"):
        parse_provenance(broken)


def test_append_provenance_keeps_the_first_record_and_recent_199() -> None:
    """The 201st continuation still lands by dropping the oldest repair."""
    records = [
        PublicationRecord(str(UUID(int=index)), f"{index:040x}")
        for index in range(1, 201)
    ]
    body = "Human prose\n" + provenance_block(records, PUBLIC_URL)
    newest = PublicationRecord(str(UUID(int=201)), "ab" * 20)
    updated = append_provenance(body, newest, PUBLIC_URL)
    parsed = parse_provenance(updated)
    assert len(parsed) == 200
    assert parsed[0] == records[0]
    assert parsed[-1] == newest
    assert records[1] not in parsed
    assert updated.startswith("Human prose\n")
    assert updated.count(PROVENANCE_START) == 1


def test_append_provenance_rejects_oversize_body() -> None:
    record = PublicationRecord(EXECUTION, HEAD)
    body = upsert_provenance("seed", [record], PUBLIC_URL)
    room = 65536 - len(body.encode("utf-8"))
    padded = body + ("h" * room)
    assert len(padded.encode("utf-8")) == 65536
    with pytest.raises(ValueError, match="provider limit"):
        append_provenance(padded, PublicationRecord(REPAIR, NEXT), PUBLIC_URL)
    assert PROVENANCE_END in padded
    assert REPAIR not in padded
