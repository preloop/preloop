"""The per-account audit hash chain: its head, and the checkpoints over it.

Three tables' worth of state, only two of them here (the third is the
``chain_seq`` / ``prev_hash`` / ``row_hash`` columns on ``audit_log`` itself).

``AuditChainState`` is one row per account and it is the sequence allocator.
Sealing takes the next sequence with a single ``UPDATE ... RETURNING``, so two
passes racing each other cannot both take seq N, and a pass whose transaction
rolls back returns the number rather than leaving a hole. It also carries
``pruned_below_seq``, which is what stops a retention purge from looking like
tampering: the purge deletes the oldest audit rows on purpose, so it raises
the floor of the range the chain can speak about, and the verifier reports
``pruned`` there instead of ``missing``.

``AuditChainCheckpoint`` is a row every N sealed rows carrying the head hash
at that point and a detached signature over it. This is the part that makes a
rewrite detectable rather than merely inconvenient. A server that can write
the database can also recompute a whole self-consistent chain; what it cannot
do is produce old checkpoints whose signatures still verify under a public key
the customer already has, if it does not hold the private key. A checkpoint a
customer wrote down last month is the anchor. Held only by us, it proves
nothing more than the chain does.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base

#: Hash of the empty chain: what ``prev_hash`` is for the first sealed row.
#: A literal rather than ``NULL`` so the canonical payload of row 1 has the
#: same shape as every other row and one code path hashes them all.
GENESIS_HASH = "0" * 64


class AuditChainState(Base):
    """One row per account: the chain head and the floor under it."""

    __tablename__ = "audit_chain_state"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    last_seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Highest sequence handed out for this account. 0 means the "
        "chain is empty.",
    )
    last_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=GENESIS_HASH,
        server_default=GENESIS_HASH,
        comment="row_hash of the row at last_seq, or the genesis hash",
    )
    last_sealed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the head last moved",
    )
    pruned_below_seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Sequences at or below this were removed by the retention "
        "purge under a stated policy. The chain cannot speak about them and "
        "a verifier must not call the gap a break.",
    )
    pruned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        """Describe the head without a lazy load."""
        return (
            f"<AuditChainState account={self.account_id} seq={self.last_seq} "
            f"pruned_below={self.pruned_below_seq}>"
        )


class AuditChainCheckpoint(Base):
    """A signed anchor over the chain, written every N sealed rows."""

    __tablename__ = "audit_chain_checkpoint"
    __table_args__ = (
        Index(
            "uq_audit_chain_checkpoint_account_seq",
            "account_id",
            "seq",
            unique=True,
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Sequence of the last row this checkpoint covers",
    )
    chain_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="row_hash at seq: the head of the chain when it was taken",
    )
    row_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Rows sealed since the previous checkpoint",
    )
    checkpointed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    signing_key_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Key that signed this checkpoint. NULL when the account had "
        "no signing key yet, which is honest about an unsigned anchor rather "
        "than pretending to one.",
    )
    signature: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Base64 Ed25519 signature over the canonical checkpoint",
    )

    def __repr__(self) -> str:
        """Describe the checkpoint without a lazy load."""
        signed = "signed" if self.signature else "unsigned"
        return (
            f"<AuditChainCheckpoint account={self.account_id} seq={self.seq} {signed}>"
        )
