"""Image rollback onto a database migrated by a newer release."""

import pytest

from scripts.init_db import (
    ROLLBACK_MESSAGE,
    stop_if_database_is_newer,
    unknown_revisions,
)

KNOWN = {"20261101_base", "20261110_next"}


def test_unknown_revisions_reports_only_revisions_missing_from_the_image() -> None:
    """protects=rollback detection; fails_when=a newer revision is missed or a
    known one is flagged; no_claim=reads no real database."""
    assert unknown_revisions({"20261110_next"}, KNOWN) == set()
    assert unknown_revisions(set(), KNOWN) == set()
    assert unknown_revisions({"20261219_newer", "20261110_next"}, KNOWN) == {
        "20261219_newer"
    }


def test_stop_prints_three_plain_lines_and_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """protects=operator message before exit; fails_when=only the Alembic id is
    shown or the exit is zero; no_claim=does not run Alembic."""
    with pytest.raises(SystemExit) as stopped:
        stop_if_database_is_newer({"20261219_newer"}, KNOWN)
    assert stopped.value.code != 0
    out = capsys.readouterr().out
    assert out.strip().splitlines() == list(ROLLBACK_MESSAGE)
    assert "newer Preloop version" in out
    assert "restore the backup" in out
    assert "cannot be rolled back" in out


def test_stop_is_silent_when_the_image_knows_the_revision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """protects=normal upgrade path; fails_when=a known revision stops migrate;
    no_claim=none beyond protects."""
    stop_if_database_is_newer({"20261101_base"}, KNOWN)
    assert capsys.readouterr().out == ""
