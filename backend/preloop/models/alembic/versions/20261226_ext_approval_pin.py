"""Pin extension approvals to content hashes.

Revision ID: 20261226_ext_approval_pin
Revises: 20261225_review_readback

Adds ``extension_approval_pin`` and ``discovered_extension.content_hash_status``.

Existing approved name approvals are pinned to the content hashes their
devices report at upgrade time, so an extension whose files change after the
upgrade reads as ``content_changed`` instead of staying approved. An approved
extension with no observed hash has nothing to pin and stays a name approval.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20261226_ext_approval_pin"
down_revision: Union[str, None] = "20261225_review_readback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Pin each approved name approval to at most 50 observed hashes, most widely
# installed first, attributed to whoever made the decision.
BACKFILL_SQL = """
INSERT INTO extension_approval_pin
    (id, approval_id, content_hash, version, note,
     pinned_by_user_id, pinned_at)
SELECT gen_random_uuid(), ranked.approval_id, ranked.content_hash,
       NULL, 'Pinned at upgrade to content observed then',
       ranked.decided_by_user_id, now()
FROM (
    SELECT a.id AS approval_id, a.decided_by_user_id, e.content_hash,
           row_number() OVER (
               PARTITION BY a.id
               ORDER BY count(DISTINCT e.workstation_fingerprint) DESC,
                        e.content_hash
           ) AS rank
    FROM extension_approval a
    JOIN discovered_extension e
      ON e.account_id = a.account_id
     AND e.agent_kind = a.agent_kind
     AND e.extension_kind = a.extension_kind
     AND e.extension_id = a.extension_id
    WHERE a.approval_state = 'approved'
      AND e.present
      AND e.content_hash IS NOT NULL
    GROUP BY a.id, a.decided_by_user_id, e.content_hash
) ranked
WHERE ranked.rank <= 50
"""


def upgrade() -> None:
    """Create the pin table, the status column, and backfill observed hashes."""
    op.add_column(
        "discovered_extension",
        sa.Column(
            "content_hash_status",
            sa.String(16),
            server_default="ok",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_discovered_extension_hash_status",
        "discovered_extension",
        "content_hash_status IN ('ok','too_large','unreadable')",
    )
    op.create_table(
        "extension_approval_pin",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "approval_id",
            UUID(as_uuid=True),
            sa.ForeignKey("extension_approval.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(80), nullable=False),
        sa.Column("version", sa.String(64)),
        sa.Column("note", sa.String(512)),
        sa.Column(
            "pinned_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "approval_id", "content_hash", name="uq_extension_approval_pin"
        ),
    )
    op.create_index("ix_extension_approval_pin_id", "extension_approval_pin", ["id"])
    op.create_index(
        "ix_extension_approval_pin_approval_id",
        "extension_approval_pin",
        ["approval_id"],
    )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """Drop pins; pinned approvals fall back to the more permissive name approval."""
    op.drop_table("extension_approval_pin")
    op.drop_constraint(
        "ck_discovered_extension_hash_status", "discovered_extension", type_="check"
    )
    op.drop_column("discovered_extension", "content_hash_status")
