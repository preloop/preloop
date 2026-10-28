"""Human consent inputs for one bounded historical result export."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from preloop.schemas.ci_execution import CiReviewRequest


class CiResultExportCreate(CiReviewRequest):
    """Expected digest and revisions protect the human's reviewed decision."""

    base_sha: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    principal_id: UUID
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
