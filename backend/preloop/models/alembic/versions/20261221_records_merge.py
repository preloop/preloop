"""Join execution outcomes, denial records and credential metadata.

Revision ID: 20261221_records_merge
Revises: 20261220_execution_failure, 20261220_denial_reason_idx,
    20261220_exec_credentials
"""

revision = "20261221_records_merge"
down_revision = (
    "20261220_execution_failure",
    "20261220_denial_reason_idx",
    "20261220_exec_credentials",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the three independently applicable revisions without changing data."""
    pass


def downgrade() -> None:
    """Restore the predecessor graph without changing data."""
    pass
