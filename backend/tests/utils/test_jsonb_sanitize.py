"""Tests for the JSONB sanitizer.

Regression coverage for the 2026-08-05 incident: an agent fetched a URL that
returned gzip binary, the body was embedded into runtime_session_activity
metadata, and Postgres rejected the NUL byte with UntranslatableCharacter.
"""

import json

import pytest

from preloop.utils.jsonb_sanitize import sanitize_for_jsonb, sanitize_string

# The exact leading bytes of the gzip response from the incident.
GZIP_MAGIC = "\x1f\x8b\x08\x00"


class TestSanitizeString:
    def test_strips_nul(self):
        assert sanitize_string("before\x00after") == "beforeafter"

    def test_strips_gzip_magic_sequence(self):
        result = sanitize_string(GZIP_MAGIC + "payload")
        assert result == "payload"
        assert "\x00" not in result

    def test_preserves_newline_carriage_return_and_tab(self):
        value = "line one\nline two\r\ncol\tcol"
        assert sanitize_string(value) == value

    def test_strips_c0_controls_but_not_the_allowed_three(self):
        assert sanitize_string("a\x01b\x1fc") == "abc"

    def test_strips_del_and_c1_controls(self):
        assert sanitize_string("a\x7fb\x85c\x9fd") == "abcd"

    def test_strips_lone_surrogates(self):
        assert sanitize_string("a\ud800b\udfffc") == "abc"

    def test_leaves_ordinary_unicode_alone(self):
        value = "hello e世界 \U0001f600"
        assert sanitize_string(value) == value

    def test_truncates_with_marker_reporting_dropped_count(self):
        result = sanitize_string("x" * 100, max_chars=10)
        assert result == "x" * 10 + "... [truncated 90 bytes]"

    def test_does_not_mark_a_string_at_the_limit(self):
        assert sanitize_string("x" * 10, max_chars=10) == "x" * 10

    def test_truncation_counts_after_stripping(self):
        result = sanitize_string("\x00" * 10 + "y" * 20, max_chars=10)
        assert result == "y" * 10 + "... [truncated 10 bytes]"


class TestSanitizeForJsonb:
    def test_sanitizes_nested_dicts_and_lists(self):
        payload = {
            "choices": [
                {"message": {"content": "body" + GZIP_MAGIC + "end"}},
                {"message": {"content": "clean"}},
            ]
        }
        result = sanitize_for_jsonb(payload)
        assert result["choices"][0]["message"]["content"] == "bodyend"
        assert result["choices"][1]["message"]["content"] == "clean"

    def test_sanitizes_dict_keys(self):
        result = sanitize_for_jsonb({"bad\x00key": "value"})
        assert result == {"badkey": "value"}

    def test_preserves_non_string_scalars(self):
        payload = {"count": 3, "cost": 1.5, "ok": True, "missing": None}
        assert sanitize_for_jsonb(payload) == payload

    def test_bool_is_not_coerced_to_int(self):
        result = sanitize_for_jsonb({"flag": True})
        assert result["flag"] is True

    def test_decodes_bytes_leniently(self):
        # Bytes are not a JSON type at all. Invalid UTF-8 (0x8b in the gzip
        # header) becomes U+FFFD, which is printable and JSONB-safe; the
        # control bytes around it are stripped.
        result = sanitize_for_jsonb({"body": b"\x1f\x8b\x08\x00data"})
        assert result["body"].endswith("data")
        assert "\x00" not in result["body"]
        assert "\x1f" not in result["body"]

    def test_normalizes_tuples_and_sets_to_lists(self):
        result = sanitize_for_jsonb({"t": ("a\x00", "b"), "s": {"c\x00"}})
        assert result["t"] == ["a", "b"]
        assert result["s"] == ["c"]

    def test_applies_the_string_cap_recursively(self):
        payload = {"outer": {"inner": "z" * 50}}
        result = sanitize_for_jsonb(payload, max_string_chars=5)
        assert result["outer"]["inner"] == "zzzzz... [truncated 45 bytes]"

    def test_handles_a_circular_reference_without_hanging(self):
        payload = {"name": "root"}
        payload["self"] = payload
        result = sanitize_for_jsonb(payload)
        assert result["name"] == "root"
        # The cycle is caught the first time the same object is re-entered.
        assert result["self"] == "[truncated: circular reference]"

    def test_bounds_deeply_nested_structures(self):
        payload = {}
        node = payload
        for _ in range(100):
            node["child"] = {}
            node = node["child"]
        result = sanitize_for_jsonb(payload, max_depth=3)
        assert "[truncated: max depth exceeded]" in repr(result)

    def test_result_is_json_serializable(self):
        payload = {
            "response": {"body": GZIP_MAGIC + "\x00binary"},
            "tokens": 10,
            "tags": ("a", "b"),
        }
        # json.dumps is the same encode path psycopg2 uses for JSONB.
        assert "\x00" not in json.dumps(sanitize_for_jsonb(payload))

    @pytest.mark.parametrize("value", ["", " ", "\n", "\t"])
    def test_edge_case_strings_survive(self, value):
        assert sanitize_for_jsonb(value) == value
