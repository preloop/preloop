from typing import Optional, List, Dict, Any
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from enum import Enum as PydanticEnum
from .tracker_scope_rule import TrackerScopeRule


class TrackerTypeSchema(str, PydanticEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    JIRA = "jira"
    BITBUCKET = "bitbucket"


class TrackerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    tracker_type: TrackerTypeSchema
    url: Optional[str] = Field(None, max_length=1000)
    api_key: str  # In Pydantic V2, consider SecretStr
    account_id: UUID
    is_active: bool = True
    is_deleted: bool = False
    is_owner_managed: bool = True
    connection_details: Optional[Dict[str, Any]] = None
    meta_data: Optional[Dict[str, Any]] = None
    subscribed_events: Optional[List[str]] = None
    jira_webhook_id: Optional[str] = None
    jira_webhook_secret: Optional[str] = None  # In Pydantic V2, consider SecretStr
    scope_rules: Optional[List[TrackerScopeRule]] = Field(default_factory=list)


class TrackerCreate(TrackerBase):
    pass


class TrackerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    tracker_type: Optional[TrackerTypeSchema] = None
    url: Optional[str] = Field(None, max_length=1000)
    api_key: Optional[str] = None
    is_active: Optional[bool] = None
    is_deleted: Optional[bool] = None
    is_owner_managed: Optional[bool] = None
    connection_details: Optional[Dict[str, Any]] = None
    meta_data: Optional[Dict[str, Any]] = None
    subscribed_events: Optional[List[str]] = None
    jira_webhook_id: Optional[str] = None
    jira_webhook_secret: Optional[str] = None
    scope_rules: Optional[List[TrackerScopeRule]] = Field(default_factory=list)


class Tracker(TrackerBase):
    id: UUID
    created_at: datetime  # from Base
    updated_at: datetime  # from Base
    # Model-specific timestamps
    created: datetime
    last_updated: datetime
    is_valid: bool
    last_validation: Optional[datetime] = None
    validation_message: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)
