"""Index the resume chain root of a flow execution.

Revision ID: 20261003_resume_root_idx
Revises: 20261002_runtime_session_cwd

The executions list and the execution page roll a review/CI repair chain up
into one cost (``project_resume_lineage``). Chain members are found by
``trigger_event_details -> '_resume' ->> 'resume_root'``, which had no index,
so every list request read and detoasted the trigger payload of every
execution in the account (issue #1197).

The index is partial: only repair turns carry a resume root, so it holds one
small entry per repair and nothing for ordinary runs. The build still reads
each row's payload once, which is why it is created CONCURRENTLY (no write
lock on ``flow_execution`` while it builds) and IF NOT EXISTS (safe to
create out of band first).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261003_resume_root_idx"
down_revision: Union[str, None] = "20261002_runtime_session_cwd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

INDEX = "ix_flow_execution_resume_root"
# Must stay textually identical to RESUME_ROOT_SQL in
# preloop.models.models.flow_execution, or the planner will not match it.
EXPRESSION = "((trigger_event_details -> '_resume') ->> 'resume_root')"


def upgrade() -> None:
    """Create the partial expression index without blocking writes."""
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            f"ON flow_execution ({EXPRESSION}) "
            f"WHERE {EXPRESSION} IS NOT NULL"
        )


def downgrade() -> None:
    """Drop the index without blocking writes."""
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
