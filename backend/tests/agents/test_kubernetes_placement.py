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
