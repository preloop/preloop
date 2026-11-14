"""Authenticator factors, recovery codes, and the optional step-up policy window.

Revision ID: 20261208_user_mfa
Revises: 20261207_user_passkey_only
"""

import sqlalchemy as sa
from alembic import op

revision = "20261208_user_mfa"
down_revision = "20261207_user_passkey_only"
branch_labels = None
depends_on = None


def _common() -> list[sa.Column]:
    """Columns shared with the ORM base."""
    return [
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Add opt-in factors and an inert edition policy field."""
    op.create_table(
        "user_mfa_factor",
        *_common(),
        sa.Column("kind", sa.String(16), server_default="totp", nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_step", sa.BigInteger()),
        sa.UniqueConstraint("user_id", "kind", name="uq_user_mfa_kind"),
    )
    op.create_table(
        "user_mfa_recovery_code",
        *_common(),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_user_mfa_recovery_code_user_id", "user_mfa_recovery_code", ["user_id"]
    )
    op.add_column(
        "account",
        sa.Column(
            "step_up_window_minutes", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_account_step_up_window",
        "account",
        "step_up_window_minutes = 0 OR step_up_window_minutes BETWEEN 5 AND 720",
    )


def downgrade() -> None:
    """Remove the optional policy field and all MFA material."""
    op.drop_constraint("ck_account_step_up_window", "account", type_="check")
    op.drop_column("account", "step_up_window_minutes")
    op.drop_table("user_mfa_recovery_code")
    op.drop_table("user_mfa_factor")
