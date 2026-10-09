"""Closed, portable account-access policy syntax; execution remains an EE hook."""

from __future__ import annotations

import re
from datetime import time
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from preloop.models.models.access_values import RULE_ACTIONS, TAGGABLE_RESOURCE_TYPES

KEY = re.compile(r"^[a-z0-9._/-]{1,63}$")
Action = Literal[
    "model:invoke",
    "tool:call",
    "flow:run",
    "runner:accept",
    "resource:view",
    "resource:share",
]
Mode = Literal["additive", "require_permit"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def tag_key(value: str) -> str:
    if not KEY.fullmatch(value):
        raise ValueError("expected a lowercase tag key of 1 to 63 characters")
    return value


class MatchExpression(ClosedModel):
    key: str
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist"]
    values: list[str] = Field(default_factory=list, max_length=100)

    _key = field_validator("key")(tag_key)

    @model_validator(mode="after")
    def validate_values(self) -> MatchExpression:
        if self.operator in {"In", "NotIn"} and not self.values:
            raise ValueError("In/NotIn require at least one value")
        if self.operator in {"Exists", "DoesNotExist"} and self.values:
            raise ValueError("Exists/DoesNotExist cannot specify values")
        if any(len(value) > 128 for value in self.values) or len(
            set(self.values)
        ) != len(self.values):
            raise ValueError("values must be distinct and at most 128 characters")
        return self


class LabelSelector(ClosedModel):
    match_labels: dict[str, str] = Field(
        default_factory=dict, alias="matchLabels", max_length=100
    )
    match_expressions: list[MatchExpression] = Field(
        default_factory=list, alias="matchExpressions", max_length=100
    )

    @field_validator("match_labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        for key, text in value.items():
            tag_key(key)
            if len(text) > 128:
                raise ValueError("tag values must be at most 128 characters")
        return value


class SubjectSelector(LabelSelector):
    kind: Literal["user", "agent", "api_key", "flow"] | None = None
    ids: list[UUID] = Field(default_factory=list, max_length=100)
    roles: list[str] = Field(default_factory=list, max_length=100)
    teams: list[str] = Field(default_factory=list, max_length=100)
    membership_kind: Literal["direct", "inherited"] | None = Field(
        None, alias="membershipKind"
    )
    home_account: LabelSelector | None = Field(None, alias="homeAccount")
    account: LabelSelector | None = None


class ResourceSelector(LabelSelector):
    account: LabelSelector | None = None


class TimeWindow(ClosedModel):
    start: time
    end: time
    timezone: str = "UTC"
    days: list[int] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("expected an IANA timezone") from None
        return value

    @field_validator("days")
    @classmethod
    def validate_days(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(day not in range(7) for day in value):
            raise ValueError("days must be distinct weekdays from 0 (Monday) to 6")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> TimeWindow:
        if self.start == self.end or self.start.tzinfo or self.end.tzinfo:
            raise ValueError("start/end must be distinct local times without offsets")
        return self


class ConditionSet(ClosedModel):
    same_tag: str | None = Field(None, alias="sameTag")
    account_relation: Literal["own", "shared", "fromParent"] | None = Field(
        None, alias="accountRelation"
    )
    time_window: TimeWindow | None = Field(None, alias="timeWindow")
    flow: LabelSelector | None = None

    @field_validator("same_tag")
    @classmethod
    def validate_same_tag(cls, value: str | None) -> str | None:
        return tag_key(value) if value is not None else None


class RuleConditions(ClosedModel):
    when: ConditionSet = Field(default_factory=ConditionSet)
    unless: ConditionSet | None = None


class AccessRuleDefinition(ClosedModel):
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    effect: Literal["permit", "forbid"]
    actions: list[Action] = Field(min_length=1, max_length=len(RULE_ACTIONS))
    subject_selector: SubjectSelector = Field(default_factory=SubjectSelector)
    resource_type: str | None = None
    resource_selector: ResourceSelector = Field(default_factory=ResourceSelector)
    conditions: RuleConditions = Field(default_factory=RuleConditions)
    scope: Literal["self", "subaccounts", "self_and_subaccounts"] = "self"
    priority: int = 0
    is_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def memo_shape(cls, raw: Any) -> Any:
        """Accept the documented subject/resource/when/unless YAML shape."""
        if not isinstance(raw, dict):
            return raw
        value = dict(raw)
        if "subject" in value:
            if "subject_selector" in value:
                raise ValueError("specify subject or subject_selector, not both")
            value["subject_selector"] = value.pop("subject")
        if "resource" in value:
            if "resource_selector" in value or "resource_type" in value:
                raise ValueError("specify resource or resource_selector/type, not both")
            resource = value.pop("resource")
            if not isinstance(resource, dict):
                raise ValueError("resource must be a selector object")
            resource = dict(resource)
            value["resource_type"] = resource.pop("type", None)
            value["resource_selector"] = resource
        if "when" in value or "unless" in value:
            if "conditions" in value:
                raise ValueError("specify conditions or when/unless, not both")
            conditions = {"when": value.pop("when", {})}
            if "unless" in value:
                conditions["unless"] = value.pop("unless")
            value["conditions"] = conditions
        return value

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, value: str | None) -> str | None:
        if value is not None and value not in TAGGABLE_RESOURCE_TYPES:
            raise ValueError("unknown resource type")
        return value

    @model_validator(mode="after")
    def validate_action_conditions(self) -> AccessRuleDefinition:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be distinct")
        if not self.name.strip():
            raise ValueError("name cannot be blank")
        conditions = [self.conditions.when, self.conditions.unless]
        if any(
            condition and condition.flow for condition in conditions
        ) and self.actions != ["runner:accept"]:
            raise ValueError("flow conditions are only valid for runner:accept rules")
        return self
