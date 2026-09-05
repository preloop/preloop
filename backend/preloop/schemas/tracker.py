"""Tracker schemas for request and response validation."""

from typing import Any, Dict, List, Optional
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, ConfigDict, computed_field

from preloop.models.crud.tracker import UNKNOWN_PROJECTS_META_KEY
from preloop.models.models.tracker import TrackerType
from .tracker_scope_rule import TrackerScopeRuleCreate, TrackerScopeRuleResponse


class TrackerBase(BaseModel):
    """Base model for tracker data."""

    name: str = Field(..., description="User-friendly name for the tracker")
    tracker_type: TrackerType = Field(..., description="Type of the issue tracker")
    url: Optional[HttpUrl] = Field(
        None, description="URL of the tracker instance (required for Jira)"
    )
    is_active: bool = Field(True, description="Whether the tracker is active")
    connection_details: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Tracker-specific connection details"
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
        None, description="URL of the tracker instance (required for Jira)"
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

    def __init__(self, **data):
        super().__init__(**data)
        print("Incoming data:", data)


class TrackerUpdate(BaseModel):
    """Model for updating an existing tracker."""

    name: Optional[str] = Field(None, description="New name for the tracker")
    url: Optional[str] = Field(None, description="New URL for the tracker instance")
    api_key: Optional[str] = Field(
        None, description="New API key or token for the tracker"
    )
    is_active: Optional[bool] = Field(None, description="New active status")
    connection_details: Optional[Dict[str, Any]] = Field(
        None, description="Updated connection details"
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


class TrackerTestRequest(BaseModel):
    """Model for testing tracker connection and listing projects."""

    tracker_id: Optional[str] = Field(
        None, description="Tracker unique identifier (UUID)"
    )
    tracker_type: TrackerType = Field(..., description="Type of the issue tracker")
    url: Optional[str] = Field(None, description="URL of the tracker instance")
    api_key: str = Field(..., description="API key or token for the tracker")
    connection_details: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Tracker-specific connection details"
    )
    organization_identifier: Optional[str] = Field(
        None, description="Identifier for the organization to fetch projects from"
    )


class ProjectIdentifier(BaseModel):
    id: str
    name: str
    identifier: str
    type: str = "project"


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
