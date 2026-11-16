"""Bounded, reference-only data returned by an external approval service."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Reference = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
FieldName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
FieldValue = Annotated[str, StringConstraints(strict=True, max_length=2048)]


class ExternalApprovalResult(BaseModel):
    """References and flat string fields; never evidence or nested objects."""

    model_config = ConfigDict(extra="forbid")

    external_reference: Reference | None = None
    grant_reference: Reference | None = None
    fields: dict[FieldName, FieldValue] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def bounded_payload(self) -> "ExternalApprovalResult":
        """Limit the UTF-8 JSON representation to 8 KiB."""
        if len(json.dumps(self.model_dump(exclude_none=True)).encode("utf-8")) > 8192:
            raise ValueError("External approval result exceeds 8 KiB")
        return self


def validate_scope_approval_config(value: dict | None) -> dict | None:
    """Validate the per-server opt-in without touching credentials."""
    from uuid import UUID

    if value and value.get("on_insufficient_scope") is not None:
        if value["on_insufficient_scope"] != "request_approval":
            raise ValueError("on_insufficient_scope must be request_approval")
        try:
            UUID(str(value.get("approval_workflow", "")))
        except ValueError:
            raise ValueError("approval_workflow must be a workflow UUID") from None
    return value


def validate_external_workflow_config(value: dict | None) -> dict | None:
    """Reject ambiguous booleans and wildcard or URL-shaped allow-list entries."""
    import re

    if not value:
        return value
    for key in ("external_approval_link", "expose_comment_to_agent"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"{key} must be true or false")
    hosts = value.get("external_approval_hosts")
    if hosts is not None:
        if not isinstance(hosts, list) or not hosts or len(hosts) > 32:
            raise ValueError("external_approval_hosts must contain 1 to 32 exact hosts")
        for host in hosts:
            if (
                not isinstance(host, str)
                or len(host) > 253
                or ".." in host
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
            ):
                raise ValueError("external_approval_hosts must contain exact DNS hosts")
    return value
