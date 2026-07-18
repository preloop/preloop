"""Pydantic schemas for notification preferences."""

import uuid
from typing import Optional, List, Dict, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotificationPreferencesBase(BaseModel):
    """Base schema for notification preferences."""

    preferred_channel: str = Field(
        "email", description="Preferred notification channel: 'email' or 'mobile_push'"
    )
    enable_email: bool = Field(
        True, description="Whether email notifications are enabled"
    )
    enable_mobile_push: bool = Field(
        False, description="Whether mobile push notifications are enabled"
    )


class NotificationPreferencesUpdate(NotificationPreferencesBase):
    """Schema for updating notification preferences."""

    preferred_channel: Optional[str] = Field(
        None, description="Preferred notification channel"
    )
    enable_email: Optional[bool] = Field(None, description="Enable email notifications")
    enable_mobile_push: Optional[bool] = Field(
        None, description="Enable mobile push notifications"
    )


class NotificationPreferencesResponse(NotificationPreferencesBase):
    """Schema for notification preferences response."""

    id: uuid.UUID
    user_id: uuid.UUID
    mobile_device_tokens: Optional[List[Dict]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MobileDeviceRegistration(BaseModel):
    """Schema for registering a mobile device."""

    platform: str = Field(..., description="Device platform: 'ios' or 'android'")
    # A blank token is silently accepted by APNs registration but then fails
    # every send with 400 MissingDeviceToken — reject it at the door.
    token: str = Field(..., min_length=1, description="Device push notification token")
    device_name: Optional[str] = Field(
        None, description="Optional device name for API key"
    )

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        """Reject blank/whitespace-only push tokens."""
        token = value.strip()
        if not token:
            raise ValueError("Device push token must not be empty")
        return token


class TestPushRequest(BaseModel):
    """Schema for triggering an admin diagnostic test push."""

    kind: Literal["approval", "question"] = Field(
        "approval",
        description="'approval' for a test approval request, 'question' for a "
        "test ask_user question",
    )
    platform: Optional[Literal["ios", "android"]] = Field(
        None, description="Restrict the test to one platform; default is all devices"
    )


class TestPushDeviceResult(BaseModel):
    """Per-device outcome of a test push send."""

    platform: str = Field(..., description="Device platform")
    token: str = Field(..., description="Masked device token")
    transport: str = Field(..., description="Delivery path used: apns/fcm/proxy/none")
    success: bool = Field(..., description="Whether the provider accepted the send")
    error: Optional[str] = Field(None, description="Verbatim provider error")
    error_reason: Optional[str] = Field(
        None, description="Machine-readable reason code"
    )
    remediation: Optional[str] = Field(None, description="Operator-facing fix hint")
    project_id: Optional[str] = Field(
        None, description="Firebase project of the server credentials (FCM only)"
    )
    status_code: Optional[int] = Field(None, description="Provider HTTP status (APNs)")
    pruned: bool = Field(False, description="Whether the dead token was removed")


class TestPushResponse(BaseModel):
    """Schema for the result of an admin diagnostic test push."""

    kind: str = Field(..., description="Kind of test sent")
    request_id: str = Field(
        ..., description="Synthetic request id; intentionally not persisted"
    )
    sent: int = Field(..., description="Number of devices that accepted the send")
    failed: int = Field(..., description="Number of devices that failed")
    results: List[TestPushDeviceResult] = Field(
        default_factory=list, description="Per-device results"
    )


class QRCodeResponse(BaseModel):
    """Schema for QR code registration response."""

    token: str = Field(..., description="Registration token")
    qr_data: str = Field(..., description="QR code data (URL)")
    expires_at: str = Field(..., description="Token expiry timestamp")
    expires_in_seconds: int = Field(..., description="Seconds until expiry")


class MobileDeviceRegistrationResponse(BaseModel):
    """Schema for mobile device registration response with API key."""

    preferences: NotificationPreferencesResponse
    api_key: str = Field(..., description="API key for mobile app authentication")
    api_key_id: uuid.UUID = Field(..., description="API key ID")
    api_key_expires_at: Optional[datetime] = Field(
        None, description="API key expiration"
    )
    push_registered: bool = Field(
        True,
        description="Whether the supplied push token was accepted and stored. "
        "False means pairing succeeded but push is not enabled for this device.",
    )
    push_error: Optional[str] = Field(
        None, description="Why the push token was rejected, if it was"
    )
