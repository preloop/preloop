"""Request and response shapes for the Delivery Report policy API (#2020)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from preloop.services.delivery_report import DeliveryReportPolicy


class DeliveryReportPolicyUpdate(BaseModel):
    """A save: the full set of overrides for this layer.

    Fields left out fall back to the layer below (the defaults for the
    account, the account for a repository). An empty object clears them.
    """

    overrides: Dict[str, Any] = Field(default_factory=dict)


class DeliveryReportPolicyResponse(BaseModel):
    """One layer of the policy and what it resolves to."""

    level: Literal["account", "project"]
    project_id: Optional[str] = None
    #: What this layer sets; empty when nothing was saved.
    overrides: Dict[str, Any]
    #: The policy a run applies here.
    effective: DeliveryReportPolicy
    #: The built-in defaults, shown before an account enables anything.
    defaults: DeliveryReportPolicy
    #: Fields a repository may override; the rest are the account's.
    project_override_fields: List[str]


class DeliveryReportPolicyRefusalDetail(BaseModel):
    """Why a save was refused."""

    #: ``close_not_allowed``, ``budget_required``, ``account_setting`` or
    #: ``invalid_policy``.
    code: str
    message: str


class DeliveryReportPolicyRefusal(BaseModel):
    """The 422 body of a refused policy save."""

    detail: DeliveryReportPolicyRefusalDetail
