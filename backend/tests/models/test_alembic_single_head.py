"""Guard: the Alembic revision graph must always have exactly one head.

This is a pure graph test. It reads the migration scripts off disk with
``alembic.script.ScriptDirectory`` and never opens a database connection, so it
runs in a plain ``pytest`` invocation with no Postgres available.

Why it exists: multiple feature branches each parented a new revision on the
same main head, then a later branch added a merge revision while another branch
turned its own DDL revision into a merge over the same parents. Both branches
passed CI in isolation; merging them produced two heads and
``alembic upgrade head`` failed with "Multiple head revisions are present",
which broke database init on main. A per-branch head count catches that at the
merge commit instead of after it lands.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

# backend/tests/models/ -> backend/preloop/models/
ALEMBIC_ROOT = Path(__file__).resolve().parents[2] / "preloop" / "models"


def _script_directory() -> ScriptDirectory:
    """Load the migration graph from disk without touching a database."""
    config = Config(str(ALEMBIC_ROOT / "alembic.ini"))
    # script_location in alembic.ini is relative to the models package.
    config.set_main_option("script_location", str(ALEMBIC_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_alembic_has_exactly_one_head() -> None:
    """Fail loudly, naming the heads, if the revision graph forked."""
    script = _script_directory()
    heads = script.get_heads()

    if len(heads) != 1:
        details = []
        for head in heads:
            revision = script.get_revision(head)
            down = revision.down_revision or "<base>"
            details.append(
                f"  - {head} ({Path(revision.path).name}, down_revision={down!r})"
            )
        raise AssertionError(
            f"Expected exactly 1 Alembic head, found {len(heads)}:\n"
            + "\n".join(details)
            + "\n\nTwo or more branches added revisions on the same parent, or "
            "two merge revisions were created for the same fork. "
            "`alembic upgrade head` cannot run in this state. Re-parent the "
            "newest revision onto the current head, or keep a single merge "
            "revision, so the graph converges again."
        )


def test_alembic_head_is_reachable_from_base() -> None:
    """Every revision must be walkable base -> head (no orphan branches)."""
    script = _script_directory()
    heads = script.get_heads()
    # A forked graph is already reported by test_alembic_has_exactly_one_head;
    # don't stack a second, more confusing failure on top of it.
    if len(heads) != 1:
        pytest.skip(f"graph has {len(heads)} heads; see the single-head test")
    head = heads[0]

    walked = {revision.revision for revision in script.walk_revisions("base", head)}
    all_revisions = {revision.revision for revision in script.walk_revisions()}

    orphans = sorted(all_revisions - walked)
    assert not orphans, (
        "Revisions are not reachable from the single head "
        f"{head!r}: {orphans}. They are detached from the migration graph."
    )
