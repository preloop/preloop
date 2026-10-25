"""Converge Live Activity and Fleet Map code activity without rewriting history."""

revision = "20261031_activity_merge"
down_revision = ("20261031_live_activity", "20261031_code_tool_idx")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both feature migrations and their indexes."""
    pass


def downgrade() -> None:
    """Leave schema rollbacks to the respective parents."""
    pass
