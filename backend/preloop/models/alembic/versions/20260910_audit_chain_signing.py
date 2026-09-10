"""Audit hash chain columns, chain state, checkpoints and account signing keys.

Revision ID: 20260910_audit_chain
Revises: 20260910_retention_hold
Create Date: 2026-09-10

Four pieces of one feature (issue #558).

``audit_log`` gains ``chain_seq``, ``prev_hash``, ``row_hash`` and
``sealed_at``, all nullable. Nullable is not laziness: every row written
before this migration has no chain position and never will, and a row written
after it is unsealed until the sealer reaches it. A NOT NULL column here would
have forced a backfill of the whole audit table inside the migration, on a
table that is one of the largest in the product.

``audit_chain_state`` is the head and the sequence allocator, one row per
account. ``audit_chain_checkpoint`` is a signed anchor every N rows.
``account_signing_key`` holds the Ed25519 key, public part in the clear and
private part Fernet-encrypted, with a partial unique index enforcing one
active key per account.

Downgrade drops all of it. That loses the chain, which cannot be recomputed
afterwards for rows that were sealed and then edited, and that is the honest
limit of reversing a tamper-evidence feature: a downgrade is itself a way to
erase the evidence, which is why the docs say the chain protects against a
careless writer and not against a platform administrator.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260910_audit_chain"
down_revision: Union[str, None] = "20260910_retention_hold"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_GENESIS_HASH = "0" * 64


def upgrade() -> None:
    """Add the chain columns and the three supporting tables."""
    op.add_column("audit_log", sa.Column("chain_seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("row_hash", sa.String(64), nullable=True))
    op.add_column(
        "audit_log",
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Serves the chain walk (account, sequence order) and the sealer's own
    # "next unsealed rows for this account" query, where NULLs sort last.
    op.create_index(
        "ix_audit_log_account_chain_seq", "audit_log", ["account_id", "chain_seq"]
    )

    op.create_table(
        "audit_chain_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "last_hash", sa.String(64), nullable=False, server_default=_GENESIS_HASH
        ),
        sa.Column("last_sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "pruned_below_seq", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_chain_state_id", "audit_chain_state", ["id"])
    # Unique, not merely indexed: the head is the sequence allocator, and two
    # head rows for one account would hand out the same sequence twice.
    op.create_index(
        "ix_audit_chain_state_account_id",
        "audit_chain_state",
        ["account_id"],
        unique=True,
    )

    op.create_table(
        "audit_chain_checkpoint",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("chain_hash", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checkpointed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signing_key_id", sa.String(64), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_chain_checkpoint_id", "audit_chain_checkpoint", ["id"])
    op.create_index(
        "ix_audit_chain_checkpoint_account_id", "audit_chain_checkpoint", ["account_id"]
    )
    op.create_index(
        "ix_audit_chain_checkpoint_checkpointed_at",
        "audit_chain_checkpoint",
        ["checkpointed_at"],
    )
    op.create_index(
        "uq_audit_chain_checkpoint_account_seq",
        "audit_chain_checkpoint",
        ["account_id", "seq"],
        unique=True,
    )

    op.create_table(
        "account_signing_key",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False, server_default="ed25519"),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("private_key_encrypted", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rotated_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_account_signing_key_id", "account_signing_key", ["id"])
    op.create_index(
        "ix_account_signing_key_account_id", "account_signing_key", ["account_id"]
    )
    op.create_index(
        "ix_account_signing_key_key_id", "account_signing_key", ["key_id"], unique=True
    )
    # One active key per account, in the index rather than in a service check,
    # for the same reason #559 put the one-active-hold rule in an index.
    op.create_index(
        "uq_account_signing_key_active",
        "account_signing_key",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the keys, the checkpoints, the head and the chain columns."""
    op.drop_index("uq_account_signing_key_active", table_name="account_signing_key")
    op.drop_index("ix_account_signing_key_key_id", table_name="account_signing_key")
    op.drop_index("ix_account_signing_key_account_id", table_name="account_signing_key")
    op.drop_index("ix_account_signing_key_id", table_name="account_signing_key")
    op.drop_table("account_signing_key")

    op.drop_index(
        "uq_audit_chain_checkpoint_account_seq", table_name="audit_chain_checkpoint"
    )
    op.drop_index(
        "ix_audit_chain_checkpoint_checkpointed_at",
        table_name="audit_chain_checkpoint",
    )
    op.drop_index(
        "ix_audit_chain_checkpoint_account_id", table_name="audit_chain_checkpoint"
    )
    op.drop_index("ix_audit_chain_checkpoint_id", table_name="audit_chain_checkpoint")
    op.drop_table("audit_chain_checkpoint")

    op.drop_index("ix_audit_chain_state_account_id", table_name="audit_chain_state")
    op.drop_index("ix_audit_chain_state_id", table_name="audit_chain_state")
    op.drop_table("audit_chain_state")

    op.drop_index("ix_audit_log_account_chain_seq", table_name="audit_log")
    op.drop_column("audit_log", "sealed_at")
    op.drop_column("audit_log", "row_hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "chain_seq")
