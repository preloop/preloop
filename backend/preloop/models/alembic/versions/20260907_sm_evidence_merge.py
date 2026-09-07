"""Merge security-maintenance and evidence-receipt Alembic heads.

Revision ID: 20260907_sm_evidence_merge
Revises: 20260907_security_maintenance, 20260907_evidence_receipt
"""

revision = "20260907_sm_evidence_merge"
down_revision = ("20260907_security_maintenance", "20260907_evidence_receipt")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both already-applied migration branches."""
    pass


def downgrade() -> None:
    """Return to the two independent heads without modifying their data."""
    pass
