"""The question-form schema subset: what it accepts, and what it refuses.

A schema the console cannot draw must be refused when the agent asks the
question, not when the human is staring at a blank form; an answer that does
not fit the schema must be refused when it is submitted, not when the agent
tries to apply it. Both directions are pinned here.
"""

from datetime import datetime

import pytest

from preloop.services.question_schema import (
    AnswerValidationError,
    QuestionSchemaError,
    apply_autofill,
    normalize_input_schema,
    normalize_items,
    question_summary,
    summarize_answer,
    validate_answer,
    validate_schema_items,
    MAX_ANSWER_BYTES,
    MAX_ITEM_ID_LENGTH,
)


WAIVER_SCHEMA = {
    "type": "object",
    "properties": {
        "waived": {
            "type": "array",
            "title": "Findings to waive",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"enum": ["CVE-2024-1", "CVE-2024-2"]},
                    "reason": {"type": "string", "minLength": 5},
                },
                "required": ["id", "reason"],
            },
        },
        "author": {"type": "string", "x-autofill": "author"},
        "date": {"type": "string", "format": "date-time", "x-autofill": "date"},
    },
}


class TestNormalizeInputSchema:
    def test_none_stays_none(self):
        assert normalize_input_schema(None) is None

    def test_waiver_schema_round_trips(self):
        schema = normalize_input_schema(WAIVER_SCHEMA)
        assert schema["properties"]["waived"]["type"] == "array"
        row = schema["properties"]["waived"]["items"]
        assert row["properties"]["id"]["enum"] == ["CVE-2024-1", "CVE-2024-2"]
        # A bare {"enum": [...]} is normalized to a string field.
        assert row["properties"]["id"]["type"] == "string"
        assert row["required"] == ["id", "reason"]

    def test_accepts_a_json_string(self):
        schema = normalize_input_schema(
            '{"type": "object", "properties": {"ok": {"type": "boolean"}}}'
        )
        assert schema["properties"]["ok"]["type"] == "boolean"

    def test_multi_select_enum_array(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "picked": {"type": "array", "items": {"enum": ["a", "b"]}}
                },
            }
        )
        assert schema["properties"]["picked"]["items"]["enum"] == ["a", "b"]

    def test_nested_group_of_scalars(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "object",
                        "properties": {"package": {"type": "string"}},
                        "required": ["package"],
                    }
                },
            }
        )
        assert (
            schema["properties"]["scope"]["properties"]["package"]["type"] == "string"
        )

    @pytest.mark.parametrize(
        "bad,reason",
        [
            ({"type": "array"}, "root must be an object"),
            ({"type": "object"}, "no properties"),
            (
                {"type": "object", "properties": {"a": {"type": "date"}}},
                "unknown scalar type",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "string", "pattern": ".*"}},
                },
                "unsupported key",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["b"],
                },
                "required names a missing property",
            ),
            (
                {
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "b": {"type": "array", "items": {"enum": ["x"]}}
                                },
                            },
                        }
                    },
                },
                "arrays do not nest",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "number", "enum": [1]}},
                },
                "enum is string-only",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "number", "x-autofill": "author"}},
                },
                "autofill is string-only",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "string", "x-autofill": "nope"}},
                },
                "unknown autofill kind",
            ),
        ],
    )
    def test_rejected(self, bad, reason):
        with pytest.raises(QuestionSchemaError):
            normalize_input_schema(bad)


class TestNormalizeItems:
    def test_rows_keep_their_facts(self):
        items = normalize_items(
            [
                {
                    "id": "CVE-2024-1",
                    "title": "urllib3 request smuggling",
                    "description": "fix in 2.2.2",
                    "severity": "high",
                    "badges": ["KEV", "pip"],
                    "href": "https://osv.dev/CVE-2024-1",
                }
            ]
        )
        assert items[0]["title"] == "urllib3 request smuggling"
        assert items[0]["badges"] == ["KEV", "pip"]

    def test_title_defaults_to_the_id(self):
        assert normalize_items([{"id": "X"}])[0]["title"] == "X"

    def test_duplicate_ids_rejected(self):
        with pytest.raises(QuestionSchemaError):
            normalize_items([{"id": "X"}, {"id": "X"}])

    def test_missing_id_rejected(self):
        with pytest.raises(QuestionSchemaError):
            normalize_items([{"title": "no id"}])

    def test_script_href_rejected(self):
        with pytest.raises(QuestionSchemaError):
            normalize_items([{"id": "X", "href": "javascript:alert(1)"}])

    def test_none_is_empty(self):
        assert normalize_items(None) == []

    def test_extra_unsupported_keys_are_dropped(self):
        items = normalize_items(
            [{"id": "X", "rank": 1, "path": "/foo", "stacks": ["python"]}]
        )
        assert items[0] == {"id": "X", "title": "X", "fields": {"path": "/foo"}}

    def test_label_is_a_title_alias_and_string_extras_are_kept(self):
        items = normalize_items(
            [
                {
                    "id": "lead-1",
                    "label": "Ada Lovelace, Analytical Engines",
                    "description": "LinkedIn: https://www.linkedin.com/in/ada",
                    "linkedin": "https://www.linkedin.com/in/ada",
                    "hubspot": "https://app.hubspot.com/contacts/1",
                    "lead_file": "leads/ada.md",
                    "empty": "  ",
                    "score": 3,
                }
            ]
        )
        assert items[0] == {
            "id": "lead-1",
            "title": "Ada Lovelace, Analytical Engines",
            "description": "LinkedIn: https://www.linkedin.com/in/ada",
            "fields": {
                "linkedin": "https://www.linkedin.com/in/ada",
                "hubspot": "https://app.hubspot.com/contacts/1",
                "lead_file": "leads/ada.md",
            },
        }

    def test_title_wins_over_label(self):
        items = normalize_items([{"id": "X", "title": "T", "label": "L"}])
        assert items[0]["title"] == "T"

    def test_item_fields_are_bounded(self):
        dropped: set = set()
        entry = {"id": "X", "k" * 65: "too long a key"}
        entry.update({f"f{i}": "v" * 2000 for i in range(15)})
        items = normalize_items([entry], dropped_keys=dropped)
        fields = items[0]["fields"]
        assert len(fields) == 12
        assert all(len(v) == 1000 for v in fields.values())
        assert "k" * 65 in dropped
        assert len(dropped) == 4

    def test_extra_keys_recorded_when_dropped_keys_provided(self):
        dropped = set()
        items = normalize_items(
            [{"id": "X", "rank": 1, "path": "/foo", "manifests": ["requirements.txt"]}],
            dropped_keys=dropped,
        )
        assert dropped == {"rank", "manifests"}
        assert items[0] == {"id": "X", "title": "X", "fields": {"path": "/foo"}}

    def test_severity_outside_vocabulary_does_not_refuse(self):
        items = normalize_items([{"id": "X", "severity": "catastrophic"}])
        assert items[0]["severity"] == "catastrophic"

    def test_severity_near_miss_values_pass_through(self):
        warning = normalize_items([{"id": "W", "severity": "warning"}])
        moderate = normalize_items([{"id": "M", "severity": "Moderate"}])
        assert warning[0]["severity"] == "warning"
        assert moderate[0]["severity"] == "Moderate"

    def test_severity_in_vocabulary_accepted_and_lowercased(self):
        items = normalize_items([{"id": "X", "severity": "HIGH"}])
        assert items[0]["severity"] == "high"

    def test_item_id_longer_than_ceiling_is_refused(self):
        with pytest.raises(QuestionSchemaError) as excinfo:
            normalize_items([{"id": "x" * (MAX_ITEM_ID_LENGTH + 1)}])
        assert f"items[0].id is longer than {MAX_ITEM_ID_LENGTH} characters" in str(
            excinfo.value
        )

    def test_item_id_at_ceiling_matches_enum(self):
        item_id = "p" * MAX_ITEM_ID_LENGTH
        items = normalize_items([{"id": item_id}])
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "selected": {"type": "array", "items": {"enum": [item_id]}}
                },
            }
        )
        validate_schema_items(schema, items)
        assert items[0]["id"] == item_id


class TestValidateSchemaItems:
    def test_schema_enum_with_unknown_item_id_rejected(self):
        schema = {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {"enum": ["item-1", "item-2", "item-orphan"]},
                }
            },
        }
        items = [{"id": "item-1"}, {"id": "item-2"}]
        with pytest.raises(QuestionSchemaError) as excinfo:
            validate_schema_items(schema, items)
        assert "names id 'item-orphan' with no matching item row" in str(excinfo.value)

    def test_schema_row_object_enum_with_unknown_item_id_rejected(self):
        schema = {
            "type": "object",
            "properties": {
                "waived": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"enum": ["item-1", "ghost-item"]},
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        }
        items = [{"id": "item-1"}]
        with pytest.raises(QuestionSchemaError) as excinfo:
            validate_schema_items(schema, items)
        assert "names id 'ghost-item' with no matching item row" in str(excinfo.value)

    def test_schema_enum_all_matching_items_accepted(self):
        schema = {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {"enum": ["item-1", "item-2"]},
                }
            },
        }
        items = [{"id": "item-1"}, {"id": "item-2"}]
        validate_schema_items(schema, items)

    def test_non_item_enum_unaffected(self):
        schema = {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["json", "yaml"],
                }
            },
        }
        items = [{"id": "item-1"}, {"id": "item-2"}]
        validate_schema_items(schema, items)

    def test_plain_multi_select_enum_unaffected_when_items_present(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"enum": ["infra", "docs"]}}
                },
            }
        )
        items = normalize_items([{"id": "F-1"}])
        validate_schema_items(schema, items)

    def test_overlapping_multi_select_enum_requires_item_rows(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "picked": {
                        "type": "array",
                        "items": {"enum": ["F-1", "ghost"]},
                    }
                },
            }
        )
        items = normalize_items([{"id": "F-1"}])
        with pytest.raises(QuestionSchemaError) as excinfo:
            validate_schema_items(schema, items)
        assert "names id 'ghost' with no matching item row" in str(excinfo.value)

    def test_top_level_id_enum_without_overlap_is_accepted(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {"id": {"type": "string", "enum": ["A", "B"]}},
            }
        )
        validate_schema_items(schema, normalize_items([{"id": "F-1"}]))


class TestValidateAnswer:
    def setup_method(self):
        self.schema = normalize_input_schema(WAIVER_SCHEMA)

    def test_accepts_a_matching_answer(self):
        cleaned = validate_answer(
            self.schema,
            {"waived": [{"id": "CVE-2024-1", "reason": "mitigated at the edge"}]},
        )
        assert cleaned["waived"][0]["id"] == "CVE-2024-1"

    def test_rejects_an_id_that_was_not_offered(self):
        with pytest.raises(AnswerValidationError) as excinfo:
            validate_answer(
                self.schema, {"waived": [{"id": "CVE-9999", "reason": "because"}]}
            )
        assert excinfo.value.errors[0]["path"] == "waived[0].id"

    def test_rejects_a_missing_required_row_field(self):
        with pytest.raises(AnswerValidationError) as excinfo:
            validate_answer(self.schema, {"waived": [{"id": "CVE-2024-1"}]})
        assert excinfo.value.errors[0]["path"] == "waived[0].reason"

    def test_rejects_a_blank_required_row_field(self):
        with pytest.raises(AnswerValidationError):
            validate_answer(
                self.schema, {"waived": [{"id": "CVE-2024-1", "reason": "   "}]}
            )

    def test_rejects_unknown_fields(self):
        with pytest.raises(AnswerValidationError) as excinfo:
            validate_answer(self.schema, {"waived": [], "smuggled": "value"})
        assert excinfo.value.errors[0]["path"] == "smuggled"

    def test_required_top_level_field(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            }
        )
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {})
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"reason": ""})
        assert validate_answer(schema, {"reason": "ok"}) == {"reason": "ok"}

    def test_boolean_and_number_types(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "notify": {"type": "boolean"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 90},
                },
            }
        )
        assert validate_answer(schema, {"notify": True, "days": 30})["days"] == 30
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"notify": "yes"})
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"days": 365})

    def test_exclusive_minimum_rejects_the_bound(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "max_cost_usd": {"type": "number", "exclusiveMinimum": 0}
                },
            }
        )
        assert validate_answer(schema, {"max_cost_usd": 0.01})["max_cost_usd"] == 0.01
        with pytest.raises(AnswerValidationError) as excinfo:
            validate_answer(schema, {"max_cost_usd": 0})
        assert "greater than" in excinfo.value.errors[0]["message"]
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"max_cost_usd": -1})

    def test_multi_select_membership(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "picked": {
                        "type": "array",
                        "items": {"enum": ["a", "b"]},
                        "minItems": 1,
                    }
                },
            }
        )
        assert validate_answer(schema, {"picked": ["a"]})["picked"] == ["a"]
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"picked": []})
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"picked": ["c"]})

    def test_oversized_answer_refused(self):
        schema = normalize_input_schema(
            {"type": "object", "properties": {"note": {"type": "string"}}}
        )
        with pytest.raises(AnswerValidationError):
            validate_answer(schema, {"note": "x" * (MAX_ANSWER_BYTES + 10)})

    def test_answer_must_exist(self):
        with pytest.raises(AnswerValidationError):
            validate_answer(self.schema, None)


class TestAutofill:
    def test_author_and_date_are_stamped_over_whatever_arrived(self):
        schema = normalize_input_schema(WAIVER_SCHEMA)
        stamped = apply_autofill(
            schema,
            {"waived": [], "author": "not-me", "date": "1999-01-01"},
            author="dimo@example.com",
            decided_at=datetime(2026, 9, 8, 12, 0, 0),
        )
        assert stamped["author"] == "dimo@example.com"
        assert stamped["date"] == "2026-09-08T12:00:00"

    def test_row_level_autofill(self):
        schema = normalize_input_schema(
            {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "author": {"type": "string", "x-autofill": "author"},
                            },
                        },
                    }
                },
            }
        )
        stamped = apply_autofill(
            schema,
            {"rows": [{"id": "a"}]},
            author="dimo",
            decided_at=datetime(2026, 1, 1),
        )
        assert stamped["rows"][0]["author"] == "dimo"

    def test_unknown_author_leaves_the_field_alone(self):
        schema = normalize_input_schema(WAIVER_SCHEMA)
        stamped = apply_autofill(schema, {"waived": []}, author=None)
        assert "author" not in stamped


class TestSummaries:
    def test_answer_summary_is_a_sentence_not_json(self):
        schema = normalize_input_schema(WAIVER_SCHEMA)
        text = summarize_answer(
            schema, {"waived": [{"id": "CVE-2024-1", "reason": "mitigated"}]}
        )
        assert text.startswith("Findings to waive:")
        assert "CVE-2024-1" in text and "mitigated" in text
        assert "{" not in text

    def test_question_summary_counts_items_and_fields(self):
        schema = normalize_input_schema(WAIVER_SCHEMA)
        assert (
            question_summary([{"id": "a"}, {"id": "b"}], schema) == "2 items, 3 fields"
        )
        assert question_summary([], None) == ""


def test_shared_item_catalog_declares_the_label_alias():
    """PR #2433 review: the catalog advertised label in prose only."""
    from preloop.services.question_schema import normalize_items
    from preloop.tools.builtin_defs import QUESTION_ITEMS_SCHEMA

    props = QUESTION_ITEMS_SCHEMA["items"]["properties"]
    assert props["label"]["type"] == "string"
    assert normalize_items([{"id": "X", "label": "L"}])[0]["title"] == "L"
