"""RuntimeClass and node placement read from the agent executor environment.

The values reach the API process through the Helm chart's deployment env
(``agentExecution.runtimeClassName`` and friends, issue #1076). These tests
pin the parsing contract: a stock install keeps every setting empty, and a
typo cannot take an agent down with it.
"""

import json

import pytest

from preloop.agents import kubernetes_placement


@pytest.fixture(autouse=True)
def clear_placement_env(monkeypatch):
    """Start every test from an unset environment."""
    for name in (
        kubernetes_placement.RUNTIME_CLASS_NAME_ENV,
        kubernetes_placement.NODE_SELECTOR_ENV,
        kubernetes_placement.TOLERATIONS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


class TestRuntimeClassName:
    def test_unset_is_empty(self):
        assert kubernetes_placement.runtime_class_name() == ""

    def test_blank_is_empty(self, monkeypatch):
        monkeypatch.setenv(kubernetes_placement.RUNTIME_CLASS_NAME_ENV, "   ")
        assert kubernetes_placement.runtime_class_name() == ""

    def test_configured_value_is_returned_verbatim(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.RUNTIME_CLASS_NAME_ENV, "kata-containers"
        )
        assert kubernetes_placement.runtime_class_name() == "kata-containers"


class TestNodeSelector:
    def test_unset_is_empty(self):
        assert kubernetes_placement.node_selector() == {}

    def test_valid_json_object_is_parsed(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.NODE_SELECTOR_ENV,
            json.dumps({"disktype": "ssd", "runtime": "kata"}),
        )
        assert kubernetes_placement.node_selector() == {
            "disktype": "ssd",
            "runtime": "kata",
        }

    def test_non_string_values_are_stringified(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.NODE_SELECTOR_ENV,
            json.dumps({"agent": 1}),
        )
        assert kubernetes_placement.node_selector() == {"agent": "1"}

    @pytest.mark.parametrize("raw", ["not-json", "[]", '"ssd"'])
    def test_invalid_json_or_wrong_type_is_ignored(self, monkeypatch, raw):
        monkeypatch.setenv(kubernetes_placement.NODE_SELECTOR_ENV, raw)
        assert kubernetes_placement.node_selector() == {}

    def test_prefix_qualified_key_is_kept(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.NODE_SELECTOR_ENV,
            json.dumps({"example.com/runtime": "kata"}),
        )
        assert kubernetes_placement.node_selector() == {"example.com/runtime": "kata"}

    def test_invalid_label_key_or_value_is_dropped(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.NODE_SELECTOR_ENV,
            json.dumps(
                {
                    "disktype": "ssd",
                    "bad key": "kata",
                    "runtime": "not a label!",
                }
            ),
        )
        # Only the valid pair survives; the invalid ones cannot fail the Job.
        assert kubernetes_placement.node_selector() == {"disktype": "ssd"}

    def test_oversized_label_value_is_dropped(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.NODE_SELECTOR_ENV,
            json.dumps({"runtime": "a" * 64}),
        )
        assert kubernetes_placement.node_selector() == {}


class TestTolerations:
    def test_unset_is_empty(self):
        assert kubernetes_placement.tolerations() == []

    def test_valid_json_list_is_parsed(self, monkeypatch):
        tolerations = [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "agents",
                "effect": "NoSchedule",
            }
        ]
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV, json.dumps(tolerations)
        )
        assert kubernetes_placement.tolerations() == tolerations

    @pytest.mark.parametrize("raw", ["not-json", "{}", '"dedicated"'])
    def test_invalid_json_or_wrong_type_is_ignored(self, monkeypatch, raw):
        monkeypatch.setenv(kubernetes_placement.TOLERATIONS_ENV, raw)
        assert kubernetes_placement.tolerations() == []

    def test_non_object_entries_are_dropped(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps([{"key": "dedicated"}, "oops", 3, None]),
        )
        assert kubernetes_placement.tolerations() == [{"key": "dedicated"}]

    def test_invalid_effect_is_dropped_not_passed_to_api(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps([{"key": "dedicated", "effect": "NoShedule"}]),
        )
        # A typo in one entry must never make Job creation fail.
        assert kubernetes_placement.tolerations() == []

    def test_invalid_operator_is_dropped(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps([{"key": "dedicated", "operator": "Tolerate"}]),
        )
        assert kubernetes_placement.tolerations() == []

    def test_string_toleration_seconds_is_coerced(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps(
                [
                    {
                        "key": "dedicated",
                        "operator": "Equal",
                        "effect": "NoExecute",
                        "tolerationSeconds": "120",
                    }
                ]
            ),
        )
        assert kubernetes_placement.tolerations() == [
            {
                "key": "dedicated",
                "operator": "Equal",
                "effect": "NoExecute",
                "tolerationSeconds": 120,
            }
        ]

    @pytest.mark.parametrize("seconds", ["soon", -5, 12.5, True])
    def test_typed_toleration_seconds_is_dropped(self, monkeypatch, seconds):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps(
                [
                    {
                        "key": "dedicated",
                        "effect": "NoExecute",
                        "tolerationSeconds": seconds,
                    }
                ]
            ),
        )
        assert kubernetes_placement.tolerations() == []

    def test_exists_operator_drops_a_disallowed_value(self, monkeypatch):
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps([{"key": "dedicated", "operator": "Exists", "value": "agents"}]),
        )
        assert kubernetes_placement.tolerations() == [
            {"key": "dedicated", "operator": "Exists"}
        ]

    def test_client_tolerations_builds_api_objects(self, monkeypatch):
        from kubernetes_asyncio import client

        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps(
                [
                    {
                        "key": "dedicated",
                        "operator": "Equal",
                        "value": "agents",
                        "effect": "NoExecute",
                        "tolerationSeconds": 60,
                    }
                ]
            ),
        )
        built = kubernetes_placement.client_tolerations(client)
        assert len(built) == 1
        assert isinstance(built[0], client.V1Toleration)
        assert built[0].key == "dedicated"
        assert built[0].operator == "Equal"
        assert built[0].value == "agents"
        assert built[0].effect == "NoExecute"
        assert built[0].toleration_seconds == 60

    @pytest.mark.parametrize(
        "entry",
        [
            {"key": "dedicated", "effect": []},
            {"key": "dedicated", "effect": {}},
            {"key": "dedicated", "operator": []},
            {"key": "dedicated", "operator": {"x": 1}},
        ],
    )
    def test_unhashable_enum_fields_are_dropped(self, monkeypatch, entry):
        # A list or object must not raise TypeError out of the enum check.
        monkeypatch.setenv(kubernetes_placement.TOLERATIONS_ENV, json.dumps([entry]))
        assert kubernetes_placement.tolerations() == []

    @pytest.mark.parametrize("operator", [None, "Equal"])
    def test_value_must_be_a_label_value(self, monkeypatch, operator):
        entry = {"key": "dedicated", "value": "not a label!", "effect": "NoSchedule"}
        if operator:
            entry["operator"] = operator
        monkeypatch.setenv(kubernetes_placement.TOLERATIONS_ENV, json.dumps([entry]))
        assert kubernetes_placement.tolerations() == []

    def test_exists_ignores_an_invalid_value(self, monkeypatch):
        # The value is discarded for Exists, so its syntax does not matter.
        monkeypatch.setenv(
            kubernetes_placement.TOLERATIONS_ENV,
            json.dumps(
                [{"key": "dedicated", "operator": "Exists", "value": "not a label!"}]
            ),
        )
        assert kubernetes_placement.tolerations() == [
            {"key": "dedicated", "operator": "Exists"}
        ]

    @pytest.mark.parametrize("effect", [None, "NoSchedule", "PreferNoSchedule"])
    def test_toleration_seconds_requires_no_execute(self, monkeypatch, effect):
        entry = {"key": "dedicated", "tolerationSeconds": 60}
        if effect:
            entry["effect"] = effect
        monkeypatch.setenv(kubernetes_placement.TOLERATIONS_ENV, json.dumps([entry]))
        assert kubernetes_placement.tolerations() == []
