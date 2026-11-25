"""Strict inputs and canonical output for Claude Code extension overlays."""

import json
import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)


def validate_source_text(value: str) -> str:
    """Match the CLI's rejection of Unicode control and format characters."""
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("Marketplace text cannot contain control or format characters")
    return value


Platform = Literal["darwin", "linux", "windows"]
Text = Annotated[
    str, Field(min_length=1, max_length=2000), AfterValidator(validate_source_text)
]
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$", max_length=128)]


class StrictModel(BaseModel):
    """Reject misspelled fields instead of silently weakening policy."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GithubSource(StrictModel):
    """Documented GitHub marketplace source."""

    source: Literal["github"]
    repo: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+/(?:[A-Za-z0-9._-]+|\*)$")]
    ref: Text | None = None
    path: Text | None = None


class GitSource(StrictModel):
    """Documented git marketplace source."""

    source: Literal["git"]
    url: Text
    ref: Text | None = None
    path: Text | None = None


class URLSource(StrictModel):
    """Documented URL source; header-bearing policies require administration."""

    source: Literal["url"]
    url: Annotated[
        str,
        Field(min_length=1, max_length=2000, pattern=r"^https?://[^\s]+$"),
        AfterValidator(validate_source_text),
    ]
    headers: dict[Text, Text] | None = None


class FileSource(StrictModel):
    """Local marketplace catalog or directory."""

    source: Literal["file", "directory"]
    path: Annotated[
        str,
        Field(min_length=1, max_length=2000, pattern=r"^(?:/|[A-Za-z]:[\\/]).+"),
        AfterValidator(validate_source_text),
    ]


class HostSource(StrictModel):
    """Allow/block matcher, never a registrable marketplace."""

    source: Literal["hostPattern"]
    host_pattern: Text = Field(alias="hostPattern")


class PathSource(StrictModel):
    """Local path matcher, never a registrable marketplace."""

    source: Literal["pathPattern"]
    path_pattern: Text = Field(alias="pathPattern")


class SkillsSource(StrictModel):
    """Policy-only skills-directory marker."""

    source: Literal["skills-dir"]


Source = Annotated[
    GithubSource
    | GitSource
    | URLSource
    | FileSource
    | HostSource
    | PathSource
    | SkillsSource,
    Field(discriminator="source"),
]


class Marketplace(StrictModel):
    """An explicit administrator-supplied source, never inferred from inventory."""

    name: Name
    state: Literal["allowed", "blocked"]
    register_on_devices: StrictBool = Field(default=False, alias="register")
    source: Source

    @model_validator(mode="after")
    def check_registration(self) -> Self:
        """Reject policy matchers and blocked sources as registration targets."""
        if self.register_on_devices and (
            self.state != "allowed"
            or self.source.source in {"hostPattern", "pathPattern", "skills-dir"}
            or isinstance(self.source, GithubSource)
            and self.source.repo.endswith("/*")
        ):
            raise ValueError(
                "Only an allowed, concrete marketplace can register on devices"
            )
        return self


class EnforcementOptions(StrictModel):
    """All restrictions are opt-in."""

    block_unapproved_plugins: StrictBool = False
    restrict_marketplaces: StrictBool = False
    managed_hooks_only: StrictBool = False
    block_user_project_skills: StrictBool = False
    disable_skill_shell: StrictBool = False
    disable_sideload_flags: StrictBool = False


class PolicyUpdate(StrictModel):
    """Whole-document replacement with separate If-Match concurrency control."""

    marketplaces: list[Marketplace] = Field(default_factory=list, max_length=50)
    options: EnforcementOptions = Field(default_factory=EnforcementOptions)

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        """Reject duplicate names and contradictory exact sources."""
        names: set[str] = set()
        states: dict[str, str] = {}
        for entry in self.marketplaces:
            if entry.name in names:
                raise ValueError("Marketplace names must be unique")
            names.add(entry.name)
            key = json.dumps(
                entry.source.model_dump(exclude_none=True, by_alias=True),
                sort_keys=True,
            )
            if key in states and states[key] != entry.state:
                raise ValueError("The same source cannot be both allowed and blocked")
            states[key] = entry.state
        return self


class EnforcementPolicy(PolicyUpdate):
    """Saved policy, or the explicit revision-zero defaults before first save."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)
    revision: int = 0
    updated_at: datetime | None = None


class OmittedKey(StrictModel):
    """A restriction not represented in the emitted settings."""

    key: str
    subject: str | None = None
    reason: str


class RenderInputs(StrictModel):
    """Provenance of the rendered account decisions."""

    policy_revision: int
    approvals_updated_at: datetime | None = None
    approval_count: int


class RenderedOverlay(StrictModel):
    """Exact UTF-8 artifact plus review metadata, suitable for offline CLI export."""

    settings_json: str
    policy_revision_hash: str
    keys: list[str]
    omitted: list[OmittedKey]
    inputs: RenderInputs
    platform: Platform
    target_path: str
    minimum_claude_code_version: str
    vendor_console_status: Literal["unverified_for_preloop_routed_agents"] = (
        "unverified_for_preloop_routed_agents"
    )
