"""Guard: Alembic revisions must not loop over data batches.

Revisions run in the Helm ``pre-upgrade`` hook, which has a five-minute
budget. A revision that walks a large table in a ``while`` loop (typically
"run a LIMIT-n statement until it reports done", often inside
``autocommit_block()``) can outlast that budget and fail the release while the
loop keeps running. Data backfills belong in ``preloop.models.backfills``: the
revision registers the backfill, a worker runs it after the deploy.

The check is deliberately simple: a revision file may not contain a Python
``while`` loop. ``for`` loops over fixed lists (for example, one
``CREATE INDEX CONCURRENTLY`` per index) are fine and not flagged. Revisions
below are already applied on every supported installation and are exempt;
do not add new entries without a reason next to them.
"""

import ast
from pathlib import Path

VERSIONS = (
    Path(__file__).resolve().parents[2] / "preloop" / "models" / "alembic" / "versions"
)

ALLOWED = {
    # In-memory alias suffix search, not a database batch loop.
    "20260822_gateway_alias_collision_audit.py",
    # Batched rewrite of legacy account policy rows; account is small.
    "20260912_history_floor.py",
    # Moves legacy credential columns into secrets under a table lock; the
    # next revision drops those columns, so it cannot run after the deploy.
    "20261110_credential_backfill.py",
}


def _while_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.While)]


def test_revisions_do_not_run_batch_loops() -> None:
    offenders = {
        path.name: lines
        for path in sorted(VERSIONS.glob("*.py"))
        if path.name not in ALLOWED and (lines := _while_lines(path))
    }
    assert not offenders, (
        "Alembic revisions must not loop over data; register a backfill in "
        f"preloop.models.backfills instead. Offending files/lines: {offenders}"
    )


def test_allow_list_entries_still_need_their_exemption() -> None:
    """Stale exemptions would silently widen the guard."""
    for name in ALLOWED:
        assert _while_lines(VERSIONS / name), f"{name} no longer needs an exemption"
