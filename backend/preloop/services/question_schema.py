"""The question form: a documented JSON Schema subset for ask_user answers.

Why this exists
---------------
``ask_user`` used to offer a list of option strings and one free-text box. A
question with any structure at all ("which of these four findings do you
waive, and why each") had nowhere to go but the free-text box, so presets
ended up instructing humans to type JSON. Typing JSON into a textarea is not
a user interface, and a hand-typed array is not evidence anybody can audit.

So a question may now carry:

* ``items``: the rows the question is about (findings, files, hosts). Each row
  is ``{id, title, description?, severity?, badges?, href?}``. The console
  renders them as a table, so the human reads the finding instead of matching
  an opaque id against a paragraph of prose.
* ``input_schema``: the shape of the answer, in the subset below. The console
  renders it as a form, the server validates the submitted answer against it,
  and the agent receives the validated JSON.

The subset (deliberately small)
-------------------------------
The root is always an object::

    {"type": "object",
     "properties": {"<name>": <field>, ...},
     "required": ["<name>", ...]}

``<field>`` is one of:

* ``{"type": "string"}`` with optional ``enum`` (renders as radios or a
  select), ``format`` (``date`` / ``date-time`` / ``textarea``),
  ``minLength`` / ``maxLength``, ``x-autofill``.
* ``{"type": "number"}`` or ``{"type": "integer"}`` with ``minimum`` /
  ``maximum``.
* ``{"type": "boolean"}`` (renders as a switch).
* ``{"type": "array", "items": {"enum": [...]}}``: multi-select. When the enum
  values are the ids of the question's ``items``, the console renders the item
  table with a checkbox per row.
* ``{"type": "array", "items": {"type": "object", "properties": {...},
  "required": [...]}}``: rows with per-row fields. When the row object has an
  ``id`` property whose ``enum`` lists the question's item ids, the console
  renders the item table with a checkbox per row plus the remaining row fields
  (a reason per waived finding) inline.
* ``{"type": "object", "properties": {...}, "required": [...]}``: a named
  group of scalar fields, one level deep.

Every field may carry ``title`` and ``description`` (rendered as the label and
the help text). Nothing else is accepted: an unknown key or an unknown type is
a rejected schema, not a silently ignored one, because a field the console
cannot draw is a field the human will not answer.

``x-autofill``
--------------
``{"x-autofill": "author"}`` and ``{"x-autofill": "date"}`` are filled by the
server from the identity that decided the request and the moment it was
decided. The console renders them read-only. A human never types who they are
into a waiver register: an identity a person can type is not an identity, and
the platform already knows both facts.

Everything here is additive. A question with no ``input_schema`` behaves
exactly as before (options + free text), and this module is never consulted
for it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

#: Hard ceilings. A question is a form for a human, not a data transfer
#: format: anything past these numbers is a mistake or an abuse, and both are
#: better refused at the edge than rendered into an unusable page.
MAX_PROPERTIES = 25
MAX_ENUM_VALUES = 200
MAX_ITEMS = 200
MAX_ANSWER_BYTES = 64 * 1024
MAX_ARRAY_ITEMS = 500
MAX_STRING_LENGTH = 8000
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1000
MAX_BADGES = 6

SCALAR_TYPES = ("string", "number", "integer", "boolean")
AUTOFILL_KINDS = ("author", "date")
STRING_FORMATS = ("date", "date-time", "textarea", "email", "uri")

_FIELD_KEYS = {
    "type",
    "title",
    "description",
    "enum",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "items",
    "properties",
    "required",
    "default",
    "x-autofill",
}

_ITEM_KEYS = {"id", "title", "description", "severity", "badges", "href"}


class QuestionSchemaError(ValueError):
    """The agent handed us a schema the console could not draw."""


class AnswerValidationError(ValueError):
    """The submitted answer does not satisfy the question's schema.

    ``errors`` is a list of ``{"path": ..., "message": ...}`` so a surface can
    put the message on the field that earned it rather than on the form.
    """

    def __init__(self, errors: List[Dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


# --------------------------------------------------------------------------
# Schema normalization (runs when the agent asks the question)
# --------------------------------------------------------------------------


def _clean_text(value: Any, limit: int) -> Optional[str]:
    """Return a trimmed string of at most ``limit`` characters, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _normalize_enum(values: Any, path: str) -> List[Any]:
    if not isinstance(values, list) or not values:
        raise QuestionSchemaError(f"{path}.enum must be a non-empty list")
    if len(values) > MAX_ENUM_VALUES:
        raise QuestionSchemaError(f"{path}.enum has more than {MAX_ENUM_VALUES} values")
    cleaned: List[Any] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            raise QuestionSchemaError(f"{path}.enum values must be strings or numbers")
        if isinstance(value, (int, float)):
            cleaned.append(value)
        else:
            text = str(value).strip()
            if not text:
                raise QuestionSchemaError(f"{path}.enum values must not be blank")
            cleaned.append(text[:MAX_STRING_LENGTH])
    if len(set(map(str, cleaned))) != len(cleaned):
        raise QuestionSchemaError(f"{path}.enum values must be unique")
    return cleaned


def _normalize_scalar(field: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Validate one scalar field and return the copy we store."""
    field_type = field.get("type")
    if field_type not in SCALAR_TYPES:
        raise QuestionSchemaError(
            f"{path}.type must be one of {', '.join(SCALAR_TYPES)}"
        )
    out: Dict[str, Any] = {"type": field_type}

    title = _clean_text(field.get("title"), MAX_TITLE_LENGTH)
    if title:
        out["title"] = title
    description = _clean_text(field.get("description"), MAX_DESCRIPTION_LENGTH)
    if description:
        out["description"] = description

    if "enum" in field:
        if field_type != "string":
            raise QuestionSchemaError(f"{path}.enum is only supported on string fields")
        out["enum"] = _normalize_enum(field["enum"], path)

    if "format" in field and field["format"] is not None:
        fmt = str(field["format"])
        if fmt not in STRING_FORMATS:
            raise QuestionSchemaError(
                f"{path}.format must be one of {', '.join(STRING_FORMATS)}"
            )
        if field_type != "string":
            raise QuestionSchemaError(
                f"{path}.format is only supported on string fields"
            )
        out["format"] = fmt

    for key in ("minLength", "maxLength"):
        if key in field and field[key] is not None:
            if field_type != "string":
                raise QuestionSchemaError(f"{path}.{key} applies to string fields only")
            try:
                out[key] = int(field[key])
            except (TypeError, ValueError):
                raise QuestionSchemaError(f"{path}.{key} must be an integer") from None

    for key in ("minimum", "maximum"):
        if key in field and field[key] is not None:
            if field_type not in ("number", "integer"):
                raise QuestionSchemaError(f"{path}.{key} applies to numbers only")
            if not isinstance(field[key], (int, float)) or isinstance(field[key], bool):
                raise QuestionSchemaError(f"{path}.{key} must be a number")
            out[key] = field[key]

    autofill = field.get("x-autofill")
    if autofill is not None:
        if autofill not in AUTOFILL_KINDS:
            raise QuestionSchemaError(
                f"{path}.x-autofill must be one of {', '.join(AUTOFILL_KINDS)}"
            )
        if field_type != "string":
            raise QuestionSchemaError(
                f"{path}.x-autofill applies to string fields only"
            )
        out["x-autofill"] = autofill

    if "default" in field and field["default"] is not None:
        out["default"] = field["default"]

    return out


def _normalize_object(
    field: Dict[str, Any], path: str, *, allow_nesting: bool
) -> Dict[str, Any]:
    """Validate an object field (root, group, or array row)."""
    properties = field.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise QuestionSchemaError(f"{path}.properties must be a non-empty object")
    if len(properties) > MAX_PROPERTIES:
        raise QuestionSchemaError(f"{path} has more than {MAX_PROPERTIES} properties")

    out: Dict[str, Any] = {"type": "object", "properties": {}}
    title = _clean_text(field.get("title"), MAX_TITLE_LENGTH)
    if title:
        out["title"] = title
    description = _clean_text(field.get("description"), MAX_DESCRIPTION_LENGTH)
    if description:
        out["description"] = description

    for name, spec in properties.items():
        if not isinstance(name, str) or not name.strip():
            raise QuestionSchemaError(f"{path} has a property with no name")
        out["properties"][name] = _normalize_field(
            spec, f"{path}.{name}", allow_nesting=allow_nesting
        )

    required = field.get("required")
    if required is not None:
        if not isinstance(required, list):
            raise QuestionSchemaError(f"{path}.required must be a list of names")
        unknown = [name for name in required if name not in out["properties"]]
        if unknown:
            raise QuestionSchemaError(
                f"{path}.required names properties that do not exist: "
                f"{', '.join(map(str, unknown))}"
            )
        out["required"] = [str(name) for name in required]

    return out


def _normalize_array(
    field: Dict[str, Any], path: str, *, allow_nesting: bool
) -> Dict[str, Any]:
    items = field.get("items")
    if not isinstance(items, dict):
        raise QuestionSchemaError(f"{path}.items must describe the array entries")

    out: Dict[str, Any] = {"type": "array"}
    title = _clean_text(field.get("title"), MAX_TITLE_LENGTH)
    if title:
        out["title"] = title
    description = _clean_text(field.get("description"), MAX_DESCRIPTION_LENGTH)
    if description:
        out["description"] = description

    for key in ("minItems", "maxItems"):
        if key in field and field[key] is not None:
            try:
                out[key] = int(field[key])
            except (TypeError, ValueError):
                raise QuestionSchemaError(f"{path}.{key} must be an integer") from None

    if "enum" in items and "type" not in items:
        out["items"] = {"enum": _normalize_enum(items["enum"], f"{path}.items")}
        return out

    item_type = items.get("type")
    if item_type == "object":
        if not allow_nesting:
            raise QuestionSchemaError(
                f"{path}.items may not nest another array of objects"
            )
        out["items"] = _normalize_object(items, f"{path}.items", allow_nesting=False)
        return out
    if item_type in SCALAR_TYPES:
        out["items"] = _normalize_scalar(items, f"{path}.items")
        return out

    raise QuestionSchemaError(
        f"{path}.items must be an enum, a scalar, or an object with properties"
    )


def _normalize_field(field: Any, path: str, *, allow_nesting: bool) -> Dict[str, Any]:
    if not isinstance(field, dict):
        raise QuestionSchemaError(f"{path} must be an object describing the field")
    unknown = set(field) - _FIELD_KEYS
    if unknown:
        raise QuestionSchemaError(
            f"{path} carries unsupported keys: {', '.join(sorted(map(str, unknown)))}"
        )
    field_type = field.get("type")
    if field_type == "object":
        if not allow_nesting:
            raise QuestionSchemaError(f"{path} nests deeper than the form can render")
        return _normalize_object(field, path, allow_nesting=False)
    if field_type == "array":
        if not allow_nesting:
            raise QuestionSchemaError(f"{path} nests deeper than the form can render")
        return _normalize_array(field, path, allow_nesting=True)
    if field_type is None and "enum" in field:
        # A bare {"enum": [...]} is the shorthand every agent writes first.
        return _normalize_scalar({**field, "type": "string"}, path)
    return _normalize_scalar(field, path)


def normalize_input_schema(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate an agent-supplied ``input_schema``; return what we store.

    Returns None when no schema was supplied (the legacy options path).
    Raises :class:`QuestionSchemaError` with a message the agent can act on.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise QuestionSchemaError(
                "input_schema must be an object, or a JSON object string"
            ) from None
    if not isinstance(raw, dict) or not raw:
        raise QuestionSchemaError("input_schema must be a non-empty object")
    if raw.get("type") not in (None, "object"):
        raise QuestionSchemaError("input_schema must be an object schema")
    return _normalize_object(
        {**raw, "type": "object"}, "input_schema", allow_nesting=True
    )


def normalize_items(raw: Any) -> List[Dict[str, Any]]:
    """Validate the rows a question is about. Empty list when none given."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise QuestionSchemaError(
                "items must be a list of objects, or a JSON list string"
            ) from None
    if not isinstance(raw, list):
        raise QuestionSchemaError("items must be a list of objects")
    if len(raw) > MAX_ITEMS:
        raise QuestionSchemaError(f"items holds more than {MAX_ITEMS} rows")

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        path = f"items[{index}]"
        if not isinstance(entry, dict):
            raise QuestionSchemaError(f"{path} must be an object")
        unknown = set(entry) - _ITEM_KEYS
        if unknown:
            raise QuestionSchemaError(
                f"{path} carries unsupported keys: "
                f"{', '.join(sorted(map(str, unknown)))}"
            )
        item_id = _clean_text(entry.get("id"), MAX_TITLE_LENGTH)
        if not item_id:
            raise QuestionSchemaError(f"{path}.id is required")
        if item_id in seen:
            raise QuestionSchemaError(f"{path}.id '{item_id}' is not unique")
        seen.add(item_id)

        row: Dict[str, Any] = {"id": item_id}
        title = _clean_text(entry.get("title"), MAX_TITLE_LENGTH)
        row["title"] = title or item_id
        description = _clean_text(entry.get("description"), MAX_DESCRIPTION_LENGTH)
        if description:
            row["description"] = description
        severity = _clean_text(entry.get("severity"), 32)
        if severity:
            row["severity"] = severity
        badges = entry.get("badges")
        if badges is not None:
            if not isinstance(badges, list):
                raise QuestionSchemaError(f"{path}.badges must be a list of strings")
            cleaned_badges = [
                text
                for text in (_clean_text(badge, 64) for badge in badges[:MAX_BADGES])
                if text
            ]
            if cleaned_badges:
                row["badges"] = cleaned_badges
        href = _clean_text(entry.get("href"), 2000)
        if href:
            # The console turns this into a link; a javascript: or data: URL in
            # an agent-supplied payload is a script the operator clicks.
            if not href.lower().startswith(("http://", "https://")):
                raise QuestionSchemaError(f"{path}.href must be http(s)")
            row["href"] = href
        out.append(row)
    return out


# --------------------------------------------------------------------------
# Answer validation (runs when the human submits the form)
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_scalar(
    spec: Dict[str, Any], value: Any, path: str, errors: List[Dict[str, str]]
) -> Any:
    field_type = spec.get("type")
    if field_type == "boolean":
        if not isinstance(value, bool):
            errors.append({"path": path, "message": "must be true or false"})
            return value
        return value
    if field_type in ("number", "integer"):
        if isinstance(value, str):
            try:
                value = float(value) if field_type == "number" else int(value)
            except ValueError:
                errors.append({"path": path, "message": "must be a number"})
                return value
        if not _is_number(value):
            errors.append({"path": path, "message": "must be a number"})
            return value
        if field_type == "integer" and float(value) != int(value):
            errors.append({"path": path, "message": "must be a whole number"})
            return value
        if "minimum" in spec and value < spec["minimum"]:
            errors.append(
                {"path": path, "message": f"must be at least {spec['minimum']}"}
            )
        if "maximum" in spec and value > spec["maximum"]:
            errors.append(
                {"path": path, "message": f"must be at most {spec['maximum']}"}
            )
        return int(value) if field_type == "integer" else value

    # string
    if not isinstance(value, str):
        errors.append({"path": path, "message": "must be text"})
        return value
    text = value.strip()
    if "enum" in spec and text not in [str(v) for v in spec["enum"]]:
        errors.append({"path": path, "message": "is not one of the offered choices"})
        return text
    if len(text) > min(spec.get("maxLength", MAX_STRING_LENGTH), MAX_STRING_LENGTH):
        errors.append(
            {
                "path": path,
                "message": f"is longer than {spec.get('maxLength', MAX_STRING_LENGTH)} characters",
            }
        )
    if "minLength" in spec and len(text) < spec["minLength"]:
        errors.append(
            {"path": path, "message": f"needs at least {spec['minLength']} characters"}
        )
    return text


def _missing(value: Any) -> bool:
    """Required means answered: null, blank text and an empty list are not."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _validate_object(
    spec: Dict[str, Any], value: Any, path: str, errors: List[Dict[str, str]]
) -> Any:
    if not isinstance(value, dict):
        errors.append({"path": path, "message": "must be an object"})
        return value
    properties: Dict[str, Any] = spec.get("properties", {})
    unknown = [name for name in value if name not in properties]
    for name in unknown:
        errors.append(
            {
                "path": f"{path}.{name}" if path else name,
                "message": "is not a field of this form",
            }
        )
    required = spec.get("required", [])
    out: Dict[str, Any] = {}
    for name, field_spec in properties.items():
        child_path = f"{path}.{name}" if path else name
        if isinstance(field_spec, dict) and field_spec.get("x-autofill"):
            # The server fills these from the decision itself. Whatever the
            # client sent is dropped rather than validated: a typed author is
            # not an author, and a missing one is not the human's mistake.
            continue
        if name not in value or value[name] is None:
            if name in required:
                errors.append({"path": child_path, "message": "is required"})
            continue
        if name in required and _missing(value[name]):
            errors.append({"path": child_path, "message": "is required"})
            continue
        out[name] = _validate_value(field_spec, value[name], child_path, errors)
    return out


def _validate_array(
    spec: Dict[str, Any], value: Any, path: str, errors: List[Dict[str, str]]
) -> Any:
    if not isinstance(value, list):
        errors.append({"path": path, "message": "must be a list"})
        return value
    if len(value) > MAX_ARRAY_ITEMS:
        errors.append(
            {"path": path, "message": f"holds more than {MAX_ARRAY_ITEMS} entries"}
        )
        return value[:MAX_ARRAY_ITEMS]
    if "minItems" in spec and len(value) < spec["minItems"]:
        errors.append(
            {"path": path, "message": f"needs at least {spec['minItems']} entries"}
        )
    if "maxItems" in spec and len(value) > spec["maxItems"]:
        errors.append(
            {"path": path, "message": f"takes at most {spec['maxItems']} entries"}
        )
    item_spec = spec.get("items", {})
    out = []
    for index, entry in enumerate(value):
        out.append(_validate_value(item_spec, entry, f"{path}[{index}]", errors))
    return out


def _validate_value(
    spec: Dict[str, Any], value: Any, path: str, errors: List[Dict[str, str]]
) -> Any:
    if "enum" in spec and "type" not in spec:
        return _validate_scalar({**spec, "type": "string"}, value, path, errors)
    field_type = spec.get("type")
    if field_type == "object":
        return _validate_object(spec, value, path, errors)
    if field_type == "array":
        return _validate_array(spec, value, path, errors)
    return _validate_scalar(spec, value, path, errors)


def validate_answer(schema: Dict[str, Any], answer: Any) -> Dict[str, Any]:
    """Return the cleaned answer, or raise :class:`AnswerValidationError`.

    Client-side validation is a courtesy to the person filling the form. This
    is the one that decides, because a decision recorded from an unvalidated
    payload is a decision nobody can rely on later.
    """
    if not isinstance(schema, dict) or not schema.get("properties"):
        raise AnswerValidationError(
            [{"path": "", "message": "this question has no answer form"}]
        )
    if answer is None:
        raise AnswerValidationError([{"path": "", "message": "an answer is required"}])
    try:
        encoded = json.dumps(answer)
    except (TypeError, ValueError):
        raise AnswerValidationError(
            [{"path": "", "message": "the answer is not JSON-serializable"}]
        ) from None
    if len(encoded.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise AnswerValidationError(
            [{"path": "", "message": f"the answer exceeds {MAX_ANSWER_BYTES} bytes"}]
        )

    errors: List[Dict[str, str]] = []
    cleaned = _validate_object(schema, answer, "", errors)
    if errors:
        raise AnswerValidationError(errors)
    return cleaned


# --------------------------------------------------------------------------
# Autofill and rendering
# --------------------------------------------------------------------------


def _autofill_object(
    spec: Dict[str, Any], value: Dict[str, Any], author: Optional[str], when: str
) -> Dict[str, Any]:
    out = dict(value)
    for name, field_spec in (spec.get("properties") or {}).items():
        if not isinstance(field_spec, dict):
            continue
        kind = field_spec.get("x-autofill")
        if kind == "author":
            if author:
                out[name] = author
            continue
        if kind == "date":
            out[name] = when
            continue
        if field_spec.get("type") == "object" and isinstance(out.get(name), dict):
            out[name] = _autofill_object(field_spec, out[name], author, when)
        elif field_spec.get("type") == "array" and isinstance(out.get(name), list):
            item_spec = field_spec.get("items") or {}
            if item_spec.get("type") == "object":
                out[name] = [
                    _autofill_object(item_spec, entry, author, when)
                    if isinstance(entry, dict)
                    else entry
                    for entry in out[name]
                ]
    return out


def apply_autofill(
    schema: Dict[str, Any],
    answer: Dict[str, Any],
    *,
    author: Optional[str],
    decided_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Stamp ``x-autofill`` fields from the deciding identity and the clock.

    Whatever the client sent for those fields is overwritten: the point of an
    auto-filled author is that it cannot be typed.
    """
    when = (decided_at or datetime.utcnow()).isoformat()
    if not isinstance(answer, dict):
        return answer
    return _autofill_object(schema, answer, author, when)


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_render_value(entry) for entry in value) or "(none)"
    if isinstance(value, dict):
        return " ".join(
            f"{name}={_render_value(entry)}"
            for name, entry in value.items()
            if entry not in (None, "")
        )
    return str(value)


def summarize_answer(schema: Dict[str, Any], answer: Dict[str, Any]) -> str:
    """One line a person reads on a timeline, from the structured answer.

    The JSON is what the agent acts on; the audit trail, the email and the
    approvals list still need a sentence, and a pretty-printed blob is not
    one.
    """
    if not isinstance(answer, dict):
        return ""
    properties = (schema or {}).get("properties") or {}
    parts: List[str] = []
    for name, value in answer.items():
        if value is None or value == [] or value == "":
            continue
        spec = properties.get(name) if isinstance(properties, dict) else None
        label = (spec or {}).get("title") or name.replace("_", " ")
        parts.append(f"{label}: {_render_value(value)}")
    return "; ".join(parts)


def question_summary(
    items: List[Dict[str, Any]], schema: Optional[Dict[str, Any]]
) -> str:
    """ "4 items, 2 fields to fill" for a list row. Empty when neither exists."""
    parts: List[str] = []
    if items:
        parts.append(f"{len(items)} item{'s' if len(items) != 1 else ''}")
    if schema and isinstance(schema.get("properties"), dict):
        count = len(schema["properties"])
        parts.append(f"{count} field{'s' if count != 1 else ''}")
    return ", ".join(parts)


def answer_field_names(schema: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """The top-level field names, in schema order."""
    if not isinstance(schema, dict):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(properties.keys())


# --------------------------------------------------------------------------
# The decision path: one entry point for every surface
# --------------------------------------------------------------------------


def question_form(
    tool_args: Any,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read ``(input_schema, items)`` off a stored approval's ``tool_args``."""
    if not isinstance(tool_args, dict):
        return None, []
    schema = tool_args.get("input_schema")
    items = tool_args.get("items")
    return (
        schema if isinstance(schema, dict) and schema.get("properties") else None,
        items if isinstance(items, list) else [],
    )


def prepare_answer(
    tool_args: Any,
    answer: Any,
    *,
    author: Optional[str] = None,
    decided_at: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate, stamp and summarize a form answer for one approval request.

    Every surface that can take a decision (console, token link, mobile) calls
    this, so the rules cannot differ between them. Returns
    ``(stored_answer, summary_line)``; ``(None, None)`` when the request has no
    form and the answer is therefore not ours to interpret. Raises
    :class:`AnswerValidationError` when the submitted answer does not fit.
    """
    schema, _items = question_form(tool_args)
    if schema is None:
        return None, None
    cleaned = validate_answer(schema, answer)
    stamped = apply_autofill(schema, cleaned, author=author, decided_at=decided_at)
    return stamped, summarize_answer(schema, stamped)
