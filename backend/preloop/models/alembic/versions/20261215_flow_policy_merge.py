"""Join integrated flow and sign-in policy branches without changing data."""

revision = "20261215_flow_policy_merge"
down_revision = (
    "20261213_flow_signin_merge",
    "20261212_sign_in_approval_merge",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Converge published revisions while preserving both features."""
    pass


def downgrade() -> None:
    """Restore the parent heads without changing credentials or policies."""
    pass
