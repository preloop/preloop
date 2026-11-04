"""Server-owned proof that a flow run delivered a pull request review (EE #202).

Written by the MCP ``update_pull_request`` tool only after the code host
accepted the review. The agent cannot author it, so a review flow that
requires publication finishes SUCCEEDED only when one of these rows exists
for its execution and its pull request.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ReviewPublicationReceipt(Base):
    """One review the code host accepted for one flow execution."""

    __tablename__ = "review_publication_receipt"

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    execution_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        index=True,
    )
    #: ``github``, ``gitlab`` or ``bitbucket``.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``owner/repo`` or the GitLab project path, lower case.
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    #: PR number or MR iid, as a string.
    pull_request: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``approve``, ``request_changes`` or ``comment``.
    review_action: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Commit the provider says the review is on, when it reports one.
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Provider review, approval or discussion id, when it returns one.
    provider_review_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
