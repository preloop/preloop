"""Keep docs/security/credentials-at-rest.md in step with the models."""

import re
from pathlib import Path

from preloop.models import models  # noqa: F401  (registers every table)
from preloop.models.credential_encryption import TARGETS
from preloop.models.models.base import Base

DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "security" / "credentials-at-rest.md"
)
#: Column names that could hold a credential and therefore need an entry.
AUDIT_PATTERN = re.compile(r"secret|token|key|password|credential", re.IGNORECASE)
ROW = re.compile(r"^\| `([a-z_0-9]+)\.([a-z_0-9]+)` \| (.+?) \|", re.MULTILINE)


def _documented() -> dict[tuple[str, str], str]:
    return {(t, c): protection for t, c, protection in ROW.findall(DOC.read_text())}


def _mapped() -> set[tuple[str, str]]:
    return {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
    }


def test_every_credential_like_column_is_documented() -> None:
    documented = _documented()
    missing = sorted(
        f"{table}.{column}"
        for table, column in _mapped()
        if AUDIT_PATTERN.search(column) and (table, column) not in documented
    )
    assert not missing, (
        "Add these columns to docs/security/credentials-at-rest.md with how "
        f"they are protected: {missing}"
    )


def test_documented_columns_still_exist() -> None:
    stale = sorted(
        f"{table}.{column}"
        for table, column in _documented()
        if (table, column) not in _mapped()
    )
    assert not stale, f"Remove or rename stale entries: {stale}"


def test_sealed_columns_match_the_backfill_targets() -> None:
    documented = _documented()
    for target in TARGETS:
        if target.selector is None:
            assert documented[(target.table, target.column)].startswith("Sealed")
        else:
            assert f"`{target.table}.{target.column}`" in DOC.read_text()
