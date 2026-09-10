"""Schemas for the audit hash chain and account signing keys (#558)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChainCheckpointRead(BaseModel):
    """One signed checkpoint over the chain head at a sequence."""

    seq: int
    chain_hash: str = Field(..., description="row_hash of the row at this sequence")
    row_count: int
    checkpointed_at: str
    signing_key_id: Optional[str] = None
    signature: Optional[str] = Field(
        None, description="Base64 Ed25519 signature, null when signing failed"
    )
    signed_payload: Dict[str, Any] = Field(
        ..., description="Canonical payload the digest was taken over"
    )
    digest: str
    signature_document: Optional[Dict[str, Any]] = Field(
        None, description="Detached signature document, ready to verify"
    )


class ChainStatusRead(BaseModel):
    """Where this account's chain is, and how far behind sealing runs."""

    enabled: bool
    head_seq: int
    head_hash: str
    last_sealed_at: Optional[str] = None
    pruned_below_seq: int = Field(
        ...,
        description=(
            "Rows below this sequence were removed by the retention purge. "
            "Their absence is policy, not tampering."
        ),
    )
    sealed_rows: int
    unsealed_rows: int = Field(
        ..., description="Written but not yet chained; sealing is a background pass"
    )
    seal_lag_seconds: int
    checkpoint_interval: int
    latest_checkpoint: Optional[ChainCheckpointRead] = None
    active_key_id: Optional[str] = None


class ChainBreakRead(BaseModel):
    """The first place the walk stopped agreeing with the chain."""

    kind: str
    seq: int
    row_id: Optional[str] = None
    detail: str


class ChainVerifyRead(BaseModel):
    """Verdict of a server side walk over a range of the chain."""

    account_id: str
    status: str = Field(..., description="ok, broken or empty")
    checked_rows: int
    start_seq: int
    end_seq: int
    head_seq: int
    pruned_below_seq: int
    unsealed_rows: int
    truncated: bool = Field(
        ..., description="True when the range was cut to the row limit"
    )
    out_of_order_rows: int = Field(
        ...,
        description=(
            "Rows whose timestamp precedes the row before them. Concurrency, "
            "not evidence of tampering on its own."
        ),
    )
    first_break: Optional[ChainBreakRead] = None
    checkpoints_verified: int
    checkpoint_failures: List[Dict[str, Any]] = Field(default_factory=list)


class ChainSegmentEntry(BaseModel):
    """One row's chain material, enough to recompute its hash."""

    seq: int
    row_id: str
    prev_hash: Optional[str] = None
    row_hash: Optional[str] = None
    payload: Dict[str, Any]


class ChainSegmentRead(BaseModel):
    """Material for a client side walk. The client decides, not the server."""

    account_id: str
    row_domain: str
    after_seq: int
    head_seq: int
    pruned_below_seq: int
    genesis_hash: str
    entries: List[ChainSegmentEntry]
    has_more: bool
    note: str


class SigningKeyRead(BaseModel):
    """The public half of one signing key. Never the private half."""

    key_id: str
    algorithm: str
    public_key: str = Field(..., description="Base64 raw Ed25519 public key")
    active: bool
    created_at: Optional[str] = None
    retired_at: Optional[str] = None


class SigningKeyListRead(BaseModel):
    """Every key this account has held, plus which one signs today."""

    active_key_id: Optional[str] = None
    signature_schema: str
    signed_bytes_format: str = Field(
        ...,
        description="Exact layout of the bytes a signature covers",
    )
    keys: List[SigningKeyRead]


class SigningKeyRotateRead(BaseModel):
    """Result of a rotation: what was retired and what signs now."""

    retired: Optional[SigningKeyRead] = None
    active: SigningKeyRead
