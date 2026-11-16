"""Join flow webhook rotation with account sign-in policy storage."""

revision = "20261213_flow_signin_merge"
down_revision = (
    "20261212_flow_approval_merge",
    "20261116_sign_in_policy",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep one migration head without changing data or credentials."""
    pass


def downgrade() -> None:
    """Restore the independent branch heads without changing data."""
    pass
