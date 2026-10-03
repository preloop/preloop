"""Tracker schemas for request and response validation."""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    ConfigDict,
    computed_field,
    model_validator,
)

from preloop.models.crud.tracker import UNKNOWN_PROJECTS_META_KEY
from preloop.models import models
from preloop.utils.bitbucket import token_expiry_status as classify_token_expiry
from .tracker_scope_rule import TrackerScopeRuleCreate, TrackerScopeRuleResponse

TrackerType = models.TrackerType

logger = logging.getLogger(__name__)


class BitbucketDCConnectionDetails(BaseModel):
    """Public, non-secret DC configuration; deployment trust is server-owned."""

    model_config = ConfigDict(extra="forbid", strict=True)

    instance_url: str = Field(..., description="Approved HTTPS origin and context path")
    version: str = Field(
        "10.2", description="Validated baseline: Bitbucket DC 10.2 LTS"
    )
    project_key: Optional[str] = None
    repository_id: Optional[int] = Field(None, gt=0)
    repository_slug: Optional[str] = None
    username: Optional[str] = Field(
        None, description="Optional reviewer user slug; never used for HTTP Basic auth"
    )


class TrackerBase(BaseModel):
    """Base model for tracker data."""

    name: str = Field(..., description="User-friendly name for the tracker")
    tracker_type: TrackerType = Field(..., description="Type of the issue tracker")
    url: Optional[HttpUrl] = Field(
        None,
        description="URL of the tracker instance (required for Jira and Bitbucket Data Center)",
    )
    is_active: bool = Field(True, description="Whether the tracker is active")
    connection_details: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Tracker-specific connection details. bitbucket_dc uses instance_url, version (10.2), "
            "project_key, repository_id (numeric, stable across rename), repository_slug, and optional reviewer username. "
            "Credentials and deployment trust settings must not be stored here."
        ),
    )
    meta_data: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Additional metadata"
    )
    subscribed_events: Optional[List[str]] = Field(
        default_factory=list,
        description="List of specific webhook event names to subscribe to. Empty list implies default/all events based on client logic.",
    )
    jira_webhook_id: Optional[str] = Field(None, description="Stored Jira Webhook ID")
    # jira_webhook_secret is intentionally not in TrackerBase to avoid accidental exposure.
    # It should be handled in specific create/update schemas if needed for input,
    # and never in response schemas.


class TrackerCreate(TrackerBase):
    """Model for creating a new tracker."""

    api_key: str = Field(..., description="API key or token for the tracker")
    scope_rules: List[TrackerScopeRuleCreate] = Field(
        default_factory=list, description="List of scope rules for the tracker"
    )


class TrackerRegisterRequest(BaseModel):
    """Request model for registering a new tracker."""

    name: str = Field(..., description="User-friendly name for the tracker")
    tracker_type: TrackerType = Field(
        ..., description="Type of the issue tracker", alias="type"
    )
    url: Optional[HttpUrl] = Field(
        None,
        description="URL of the tracker instance (required for Jira and Bitbucket Data Center)",
    )
    api_key: str = Field(
        ..., description="API key or token for the tracker", alias="token"
    )
    connection_details: Optional[Dict[str, Any]] = Field(
        None, description="Tracker-specific connection details", alias="config"
    )

    # Fields needed for the base tracker model
    is_active: bool = Field(True, description="Whether the tracker is active")
    meta_data: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Additional metadata"
    )
    scope_rules: List[TrackerScopeRuleCreate] = Field(
        default_factory=list, description="List of scope rules for the tracker"
    )

    model_config = ConfigDict(
        populate_by_name=True,  # Enables the alias functionality
        json_schema_extra={
            "examples": [
                {
                    "name": "GitHub",
                    "type": "github",
                    "url": "",
                    "token": "your_token",
                    "config": None,
                }
            ]
        },
    )


class TrackerUpdate(BaseModel):
    """Model for updating an existing tracker."""

    name: Optional[str] = Field(None, description="New name for the tracker")
    url: Optional[str] = Field(None, description="New URL for the tracker instance")
    api_key: Optional[str] = Field(
        None, description="New API key or token for the tracker"
    )
    is_active: Optional[bool] = Field(None, description="New active status")
    connection_details: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Updated connection details. The legacy key 'config' is still "
            "accepted. A null connection_details is treated as absent and "
            "falls back to config. When both are objects, connection_details "
            "wins."
        ),
    )

    meta_data: Optional[Dict[str, Any]] = Field(None, description="Updated metadata")
    scope_rules: Optional[List[TrackerScopeRuleCreate]] = Field(
        None, description="Updated list of scope rules for the tracker"
    )
    subscribed_events: Optional[List[str]] = Field(
        None,
        description="Updated list of specific webhook event names to subscribe to.",
    )
    jira_webhook_id: Optional[str] = Field(None, description="Updated Jira Webhook ID")
    jira_webhook_secret: Optional[str] = Field(
        None,
        description="Updated Secret for Jira webhook validation (handle with care)",
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_config(cls, data: Any) -> Any:
        """Copy a legacy ``config`` object onto ``connection_details``.

        The console used to send ``config``. Updates persist
        ``connection_details``. Both keys are accepted during the
        deprecation window. A null ``connection_details`` is absent and
        falls back to ``config``, matching registration. When both values
        are objects, ``connection_details`` wins. A non-object ``config``
        is ignored so it cannot wipe stored details.

        Args:
            data: The raw update payload.

        Returns:
            The payload, with ``connection_details`` filled from ``config``
            when the new key was omitted.
        """
        if not isinstance(data, dict):
            return data
        # Null matches registration: the key is absent, so config can fill it.
        if data.get("connection_details") is None and "connection_details" in data:
            data = {
                key: value for key, value in data.items() if key != "connection_details"
            }
        if data.get("connection_details") is not None:
            return data
        if not isinstance(data.get("config"), dict):
            return data
        logger.info(
            "Tracker update used deprecated 'config'; send 'connection_details'"
        )
        merged = dict(data)
        merged["connection_details"] = data["config"]
        return merged


class TrackerResponse(TrackerBase):
    """Response model for tracker data (excluding sensitive info like api_key)."""

    id: UUID = Field(..., description="Tracker unique identifier (UUID)")
    account_id: UUID = Field(..., description="Account ID owning this tracker")
    is_valid: bool = Field(False, description="Whether the connection is validated")
    last_validation: Optional[datetime] = Field(
        None, description="Timestamp of the last validation attempt"
    )
    validation_message: Optional[str] = Field(
        None, description="Result message from the last validation"
    )
    created: datetime = Field(..., description="Creation timestamp")
    last_updated: datetime = Field(..., description="Last update timestamp")
    scope_rules: List[TrackerScopeRuleResponse] = Field(
        default_factory=list, description="List of scope rules for the tracker"
    )
    auth_type: str = Field(
        "api_token",
        description="How the tracker authenticates: 'api_token', 'github_app' or 'oauth_app'",
    )
    oauth_installation_id: Optional[UUID] = Field(
        None,
        description="OAuth App installation this tracker is bound to (OAuth auth types only)",
    )
    github_installation_target_login: Optional[str] = Field(
        None,
        description="Login of the account the bound installation targets (OAuth auth types only)",
    )

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def degraded_projects(self) -> List[str]:
        """Project identifiers seen on webhooks that never resolved.

        These are repositories/projects the tracker receives webhooks for but
        which repeated syncs could not import -- almost always because the
        integration's scope excludes them (GitHub App installed on selected
        repositories only, or an EXCLUDE scope rule). Surfaced so the user can
        fix the scope instead of the sync retrying forever.
        """
        unknown = (self.meta_data or {}).get(UNKNOWN_PROJECTS_META_KEY) or {}
        if not isinstance(unknown, dict):
            return []
        return sorted(
            identifier
            for identifier, entry in unknown.items()
            if isinstance(entry, dict) and entry.get("degraded")
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_expires_at(self) -> Optional[str]:
        """When the stored token expires, if the user recorded it.

        Bitbucket API tokens and repository access tokens carry an expiry
        date chosen at creation. The API does not report it, so the tracker
        form stores it in ``connection_details["token_expires_at"]``.
        """
        value = (self.connection_details or {}).get("token_expires_at")
        return str(value) if value else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_expiry_status(self) -> Optional[str]:
        """``expired``, ``expiring`` (within 14 days), ``ok`` or None."""
        return classify_token_expiry(self.token_expires_at)


class TrackerTestRequest(BaseModel):
    """Model for testing tracker connection and listing projects."""

    tracker_id: Optional[str] = Field(
        None, description="Tracker unique identifier (UUID)"
    )
    tracker_type: TrackerType = Field(..., description="Type of the issue tracker")
    url: Optional[str] = Field(None, description="URL of the tracker instance")
    api_key: str = Field(..., description="API key or token for the tracker")
    connection_details: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Tracker-specific connection details. bitbucket_dc uses instance_url, version (10.2), "
            "project_key, repository_id (numeric, stable across rename), repository_slug, and optional reviewer username. "
            "Credentials and deployment trust settings must not be stored here."
        ),
    )
    organization_identifier: Optional[str] = Field(
        None, description="Identifier for the organization to fetch projects from"
    )
    auth_type: Optional[str] = Field(
        None,
        description=(
            "Authentication mode for trackers that support several "
            "(Bitbucket Cloud: 'api_token' or 'oauth_token'; Data Center: 'api_token' user PAT only). Ignored when "
            "tracker_id is set: the stored mode is used."
        ),
    )


class ProjectIdentifier(BaseModel):
    id: str
    name: str
    identifier: str
    type: str = "project"
    group: Optional[str] = Field(
        None,
        description=(
            "Grouping label inside the organization, for example the "
            "Bitbucket project a repository belongs to."
        ),
    )


class OrganizationGroup(BaseModel):
    id: str
    name: str
    type: str = "organization"
    children: List[ProjectIdentifier] = Field(default_factory=list)


class TrackerTestResponse(BaseModel):
    """Response model for testing tracker connection."""

    success: bool = Field(..., description="Whether the connection test was successful")
    message: str = Field(..., description="Connection test result message")
    orgs: Optional[List[OrganizationGroup]] = Field(
        None,
        description="List of organizations if connection succeeded",
    )
