"""Add verified chat connections, link proofs and durable work queue."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261002_chat_connections"
down_revision = "20261002_managed_oauth"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def _account() -> sa.Column:
    return sa.Column(
        "account_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )


def _connection() -> sa.Column:
    return sa.Column(
        "connection_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("chat_connection.id", ondelete="CASCADE"),
        nullable=False,
    )


def _user(nullable: bool = False) -> sa.Column:
    return sa.Column(
        "user_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("user.id", ondelete="SET NULL" if nullable else "CASCADE"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "chat_connection",
        *_base(),
        _account(),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("workspace_id", sa.String(256), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_index("ix_chat_connection_account_id", "chat_connection", ["account_id"])
    op.create_table(
        "chat_identity",
        *_base(),
        _account(),
        _connection(),
        _user(),
        sa.Column("external_user_id", sa.String(256), nullable=False),
        sa.UniqueConstraint("connection_id", "external_user_id"),
        sa.UniqueConstraint("connection_id", "user_id"),
    )
    op.create_index("ix_chat_identity_account_id", "chat_identity", ["account_id"])
    op.create_table(
        "chat_link_code",
        *_base(),
        _account(),
        _connection(),
        _user(),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "chat_work",
        *_base(),
        _account(),
        _connection(),
        _user(True),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("external_user_id", sa.String(256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(160), nullable=True),
        sa.Column("provider_message_id", sa.String(256), nullable=True),
        sa.UniqueConstraint("connection_id", "event_id"),
    )
    op.create_index("ix_chat_work_account_id", "chat_work", ["account_id"])
    op.create_index("ix_chat_work_status", "chat_work", ["status"])


def downgrade() -> None:
    for table in ("chat_work", "chat_link_code", "chat_identity", "chat_connection"):
        op.drop_table(table)
