"""Plan each agent usage trigger statement for its own transition table.

Revision ID: 20261112_agent_usage_replan
Revises: 20261110_media_timing

PL/pgSQL keeps a statement's plan for the session, and the planner sizes a
transition table from its row count at planning time. A plan cached by
single-row inserts joins ``inserted_usage`` to the claimed ids with a nested
loop, so a later bulk insert on the same connection runs quadratically. The
three statement triggers now run each statement through ``EXECUTE``, which
plans it for the rows of that call. The SQL itself is unchanged.
"""

import importlib
import re

from alembic import op

revision = "20261112_agent_usage_replan"
down_revision = "20261110_media_timing"
branch_labels = None
depends_on = None

FUNCTIONS = (
    "agent_usage_track_insert",
    "agent_usage_track_delete",
    "agent_usage_track_update",
)


def _original(name: str) -> str:
    """Return the function definition installed by 20261109_agent_usage_totals."""
    ddl = importlib.import_module(
        "preloop.models.alembic.versions.20261109_agent_usage_totals"
    ).DDL
    match = re.search(
        rf"CREATE OR REPLACE FUNCTION {name}\(\).*?\nEND \$\$;", ddl, re.S
    )
    if match is None:
        raise RuntimeError(f"{name} not found in 20261109_agent_usage_totals")
    return match.group(0)


def _replanned(definition: str) -> str:
    """Wrap every statement of a trigger body in a dynamic ``EXECUTE``."""
    head, body = definition.split("$$\nBEGIN\n", 1)
    body, tail = body.rsplit(" RETURN NULL;\nEND $$;", 1)
    statements = [part for part in body.split(";\n") if part.strip()]
    if any("$stmt$" in part for part in statements):
        raise RuntimeError("statement already contains the $stmt$ quote")
    wrapped = "".join(f" EXECUTE $stmt${part}$stmt$;\n" for part in statements)
    return f"{head}$$\nBEGIN\n{wrapped} RETURN NULL;\nEND $$;{tail}"


def upgrade() -> None:
    """Replace the three trigger functions with dynamically planned bodies."""
    for name in FUNCTIONS:
        op.execute(_replanned(_original(name)))


def downgrade() -> None:
    """Restore the statically planned trigger functions."""
    for name in FUNCTIONS:
        op.execute(_original(name))
