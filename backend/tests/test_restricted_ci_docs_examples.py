"""Every JSON example in the restricted CI guides must be accepted as written.

A reader following docs/guide/flows/restricted-ci.md copies these blocks into
API requests. Each ```json block carries a marker comment on the line before
it, ``<!-- example: KIND -->``, naming the request model it must satisfy.
Placeholders such as ``"<project A id>"`` are replaced by a synthetic UUID
when the field holds an id, and kept as text otherwise.
"""

import json
import re
import uuid
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, TypeAdapter

from preloop.models.schemas.flow import FlowCreate, FlowUpdate, ScheduleConfig
from preloop.schemas.ci_administration import (
    CiAdminCreate,
    CiAdminPreview,
    CiAdminSubscription,
    CiAdminUpdate,
    CiSharedFlowUpdate,
)

DOCS = Path(__file__).parents[2] / "docs" / "guide" / "flows"
GUIDES = ("restricted-ci.md", "repo-review-presets.md")

# ``fragment``: a response, a partial object or a payload of another API;
# shown for reading, not sent as a request on its own.
MODELS: dict[str, Any] = {
    "flow-create": FlowCreate,
    "flow-update": FlowUpdate,
    "ci-create": CiAdminCreate,
    "ci-preview": CiAdminPreview,
    "ci-update": CiAdminUpdate,
    "ci-subscribe": CiAdminSubscription,
    "shared-flow": CiSharedFlowUpdate,
    "schedule": ScheduleConfig,
    "fragment": None,
}

FENCE = re.compile(
    r"^(?P<indent>[ \t]*)<!-- example: (?P<kind>[a-z-]+) -->\n"
    r"(?P=indent)```json\n(?P<body>.*?)\n(?P=indent)```",
    re.MULTILINE | re.DOTALL,
)
PLACEHOLDER = re.compile(r"^<[^<>]+>$")


def _examples() -> list[tuple[str, str, str]]:
    found = []
    for guide in GUIDES:
        text = (DOCS / guide).read_text(encoding="utf-8")
        marked = FENCE.findall(text)
        total = len(re.findall(r"^[ \t]*```json$", text, re.MULTILINE))
        assert len(marked) == total, (
            f"{guide}: every ```json block needs an <!-- example: KIND --> "
            f"marker on the line before it ({len(marked)} of {total} marked)"
        )
        for index, (_indent, kind, body) in enumerate(marked):
            found.append((f"{guide}#{index}:{kind}", kind, body))
    return found


EXAMPLES = _examples()


def _fill(value: Any, key: str = "") -> Any:
    """Replace ``"<...>"`` placeholders; ids become a stable synthetic UUID."""
    if isinstance(value, dict):
        return {name: _fill(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, key) for item in value]
    if isinstance(value, str) and PLACEHOLDER.match(value):
        if key.endswith("_id") or key.endswith("_ids"):
            return str(uuid.uuid5(uuid.NAMESPACE_URL, value))
        if key == "expires_at":
            return "2030-01-01T00:00:00Z"
        if key == "url":
            return "https://example.com/preloop-completion"
    return value


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    """Pydantic models inside an annotation such as ``Optional[List[M]]``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _unknown_keys(model: Any, data: Any, path: str = "") -> set[str]:
    """Keys the model would silently drop, at any depth of nested models."""
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return set()
    if not isinstance(data, dict):
        return set()
    unknown = {f"{path}{key}" for key in data if key not in model.model_fields}
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            continue
        items = value if isinstance(value, list) else [value]
        for nested in _models_in(field.annotation):
            for item in items:
                unknown |= _unknown_keys(nested, item, f"{path}{key}.")
    return unknown


def _parse(body: str) -> Any:
    """Parse a block; a fragment may be one ``"key": value`` member."""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads("{" + body + "}")


@pytest.mark.parametrize(
    ("name", "kind", "body"), EXAMPLES, ids=[item[0] for item in EXAMPLES]
)
def test_documented_json_is_accepted(name: str, kind: str, body: str) -> None:
    assert kind in MODELS, f"{name}: unknown example kind {kind!r}"
    model = MODELS[kind]
    if model is None:
        _parse(body)
        return
    data = json.loads(body)
    filled = _fill(data)
    # FlowCreate ignores unknown keys; a misspelt key would silently do
    # nothing, so the example must use real field names only.
    assert not _unknown_keys(model, filled), (name, _unknown_keys(model, filled))
    TypeAdapter(model).validate_python(filled)
    if kind == "flow-create":
        # Without a model the flow silently uses the account default.
        assert filled.get("ai_model_id"), f"{name}: set ai_model_id"


def test_guides_have_complete_flow_creation_examples() -> None:
    kinds = [kind for _name, kind, _body in EXAMPLES]
    assert kinds.count("flow-create") >= 3
    assert "ci-preview" in kinds and "ci-subscribe" in kinds


def test_nested_typo_is_caught() -> None:
    data = {"git_clone_config": {"enabled": True, "publication_mod": "read_only"}}
    assert _unknown_keys(FlowUpdate, data) == {"git_clone_config.publication_mod"}
