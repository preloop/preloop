"""Schemas for live pull request / merge request lists."""

from typing import List, Optional

from pydantic import BaseModel, Field


class PullRequestItem(BaseModel):
    """One open pull request or merge request from the tracker."""

    number: int
    iid: int
    title: str
    description: Optional[str] = None
    url: str
    author: Optional[str] = None
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    state: str
    draft: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PullRequestListResponse(BaseModel):
    """Paginated live list of open pull/merge requests."""

    items: List[PullRequestItem]
    page: int
    limit: int
    has_more: bool
    supported: bool
    fetched_at: str = Field(..., description="ISO timestamp of the fetch")
