"""Inputs, outputs and catalog entries for the flow state tools."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from preloop.models.crud.flow_state import (
    MAX_KEYS_PER_FLOW,
    MAX_VALUE_BYTES,
    RESERVED_PREFIX,
)

KEY_FIELD = Field(
    min_length=1,
    max_length=128,
    pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
    description="State key: a-z, 0-9, '.', '-', '_'.",
)


class _Closed(BaseModel):
    """Reject unknown fields."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GetFlowState(_Closed):
    """Read one key, or list the keys when ``key`` is omitted."""

    key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
        description="Key to read. Omit to list the flow's keys.",
    )


class PutFlowState(_Closed):
    """Create or replace one key."""

    key: str = KEY_FIELD
    value: Any = Field(description="Any JSON value.")
    expected_version: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Compare-and-set: 0 writes only if the key is new; N writes only "
            "if the stored version is N. Omit to overwrite."
        ),
    )


class FlowStateEntry(BaseModel):
    """One stored value and its metadata."""

    key: str
    value: Any = None
    version: int
    size_bytes: int
    updated_at: str | None = None
    updated_by_execution_id: str | None = None
    reserved: bool = False


class FlowStateList(BaseModel):
    """The flow's keys without their values."""

    keys: list[FlowStateEntry]
    max_keys: int = MAX_KEYS_PER_FLOW
    max_value_bytes: int = MAX_VALUE_BYTES


class FlowStatePutBody(_Closed):
    """REST body for ``PUT /flows/{flow_id}/state/{key}``."""

    value: Any
    expected_version: int | None = Field(default=None, ge=0)


def _tool(name: str, description: str, request: type[BaseModel]) -> dict[str, Any]:
    """Build one catalog entry; the request model is the wire schema."""
    return {
        "name": name,
        "description": description,
        "source": "builtin",
        "default_enabled": False,
        "requires_tracker": False,
        "required_tracker_types": [],
        "schema": request.model_json_schema(),
    }


FLOW_STATE_TOOLS = (
    _tool(
        "get_flow_state",
        "Read state this flow saved in earlier runs (a ledger, cursor or "
        "baseline). Pass key to read one value; omit it to list keys. A "
        "missing key returns value null and version 0. Only works inside a "
        "flow run.",
        GetFlowState,
    ),
    _tool(
        "put_flow_state",
        "Save a JSON value under key for this flow's next runs, at most "
        f"{MAX_VALUE_BYTES // 1024} KB per key and {MAX_KEYS_PER_FLOW} keys per "
        "flow. Use expected_version from get_flow_state to avoid overwriting "
        f"a concurrent run. Keys starting with '{RESERVED_PREFIX}' are written "
        "by the platform and are read-only here.",
        PutFlowState,
    ),
)
