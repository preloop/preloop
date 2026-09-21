"""Resolving a model's context window and output ceiling (#851).

A harness that does not know its window compacts at the wrong moment and
spends the run re-reading what it dropped. These tests pin where the two
numbers come from, which source wins, and what happens when neither knows:
nothing is guessed, because a wrong window is worse than no window.
"""

import logging

import pytest

from preloop.services import model_context_limits as limits_module
from preloop.services.model_context_limits import (
    ModelContextLimits,
    catalog_candidates,
    limits_for_execution,
    resolve_model_context_limits,
)

# A stand-in for the vendored snapshot, keyed the several ways the real file
# keys models. Synthetic ids only: the real catalog is exercised once, at the
# bottom, to prove the fields it carries are the ones read here.
SYNTHETIC_CATALOG = {
    "acme-large": {
        "max_input_tokens": 900000,
        "max_output_tokens": 64000,
        "max_tokens": 64000,
    },
    "acme/acme-small": {
        "max_input_tokens": 128000,
        "max_output_tokens": 8192,
    },
    "acme.acme-dotted": {
        "max_input_tokens": 200000,
        "max_output_tokens": 32000,
    },
    "acme-legacy": {
        # Older rows only carry the pre-split spelling of the output ceiling.
        "max_input_tokens": 32000,
        "max_tokens": 4096,
    },
    "acme-window-only": {
        "max_input_tokens": 500000,
    },
    "acme-nonsense": {
        "max_input_tokens": "plenty",
        "max_output_tokens": True,
    },
}


@pytest.fixture(autouse=True)
def synthetic_catalog(monkeypatch):
    """Read the synthetic snapshot instead of the vendored one."""
    monkeypatch.setattr(limits_module, "_catalog", lambda: SYNTHETIC_CATALOG)


class TestCatalogLookup:
    """What the vendored snapshot answers, and how it is keyed."""

    def test_both_numbers_come_from_the_catalog(self):
        limits = resolve_model_context_limits(model_identifier="acme-large")
        assert limits.context_window == 900000
        assert limits.max_output_tokens == 64000
        assert limits.context_window_source == "catalog"
        assert limits.max_output_tokens_source == "catalog"
        assert limits.catalog_key == "acme-large"
        assert limits.known is True

    def test_provider_qualified_key_is_tried(self):
        """The snapshot keys some models ``provider/model``."""
        limits = resolve_model_context_limits(
            model_identifier="acme-small", provider_name="acme"
        )
        assert limits.context_window == 128000
        assert limits.max_output_tokens == 8192
        assert limits.catalog_key == "acme/acme-small"

    def test_dotted_key_is_tried(self):
        """And some it keys ``provider.model``."""
        limits = resolve_model_context_limits(
            model_identifier="acme-dotted", provider_name="acme"
        )
        assert limits.context_window == 200000
        assert limits.catalog_key == "acme.acme-dotted"

    def test_qualified_identifier_falls_back_to_the_bare_model(self):
        """``acme/acme-large`` is the same model as ``acme-large``."""
        limits = resolve_model_context_limits(model_identifier="acme/acme-large")
        assert limits.context_window == 900000

    def test_gateway_alias_is_tried_before_the_identifier(self):
        """A gateway run names the model by its alias."""
        limits = resolve_model_context_limits(
            model_identifier="internal-deployment-7",
            model_alias="acme/acme-large",
        )
        assert limits.context_window == 900000
        assert limits.catalog_key == "acme-large"

    def test_older_rows_use_max_tokens_for_the_output_ceiling(self):
        limits = resolve_model_context_limits(model_identifier="acme-legacy")
        assert limits.max_output_tokens == 4096

    def test_a_catalog_entry_may_know_only_the_window(self):
        limits = resolve_model_context_limits(model_identifier="acme-window-only")
        assert limits.context_window == 500000
        assert limits.max_output_tokens is None
        assert limits.max_output_tokens_source is None
        assert limits.known is True

    def test_unusable_catalog_values_are_ignored(self):
        """A string window and a boolean ceiling are not numbers."""
        limits = resolve_model_context_limits(model_identifier="acme-nonsense")
        assert limits.context_window is None
        assert limits.max_output_tokens is None
        assert limits.known is False

    def test_a_model_nobody_knows_resolves_to_nothing(self):
        """Nothing is guessed: the caller must leave the setting out."""
        limits = resolve_model_context_limits(
            model_identifier="never-heard-of-it", provider_name="example"
        )
        assert limits == ModelContextLimits()
        assert limits.known is False

    def test_no_identifier_at_all_resolves_to_nothing(self):
        assert resolve_model_context_limits(model_identifier=None).known is False
        assert resolve_model_context_limits(model_identifier="  ").known is False


class TestModelRowWins:
    """An operator who wrote a number meant it."""

    def test_the_model_row_beats_the_catalog(self):
        """A provisioned deployment may have a smaller window than the
        public model the catalog describes."""
        limits = resolve_model_context_limits(
            model_identifier="acme-large",
            model_parameters={"context_window": 262144, "max_output_tokens": 16384},
        )
        assert limits.context_window == 262144
        assert limits.max_output_tokens == 16384
        assert limits.context_window_source == "model_row"
        assert limits.max_output_tokens_source == "model_row"

    def test_each_field_is_resolved_on_its_own(self):
        """A row that pins only the ceiling still gets its window from the
        catalog."""
        limits = resolve_model_context_limits(
            model_identifier="acme-large",
            model_parameters={"max_output_tokens": 2048},
        )
        assert limits.context_window == 900000
        assert limits.context_window_source == "catalog"
        assert limits.max_output_tokens == 2048
        assert limits.max_output_tokens_source == "model_row"

    def test_alternative_spellings_are_read(self):
        """Operators wrote these keys before this feature existed."""
        limits = resolve_model_context_limits(
            model_identifier="unknown-model",
            model_parameters={
                "max_input_tokens": 300000,
                "model_max_output_tokens": 9000,
            },
        )
        assert limits.context_window == 300000
        assert limits.max_output_tokens == 9000

    def test_nonsense_in_the_row_falls_through_to_the_catalog(self):
        """``True`` is not a context window, and 12 is a typo."""
        limits = resolve_model_context_limits(
            model_identifier="acme-large",
            model_parameters={"context_window": True, "max_output_tokens": 12},
        )
        assert limits.context_window == 900000
        assert limits.max_output_tokens == 64000
        assert limits.context_window_source == "catalog"

    def test_a_row_that_is_not_a_mapping_is_ignored(self):
        limits = resolve_model_context_limits(
            model_identifier="acme-large", model_parameters=["context_window", 1]
        )
        assert limits.context_window == 900000

    def test_a_numeric_string_from_a_json_column_still_counts(self):
        limits = resolve_model_context_limits(
            model_identifier="unknown-model",
            model_parameters={"context_window": "196608"},
        )
        assert limits.context_window == 196608
        assert limits.context_window_source == "model_row"


class TestCandidateOrder:
    """The lookup tries the spellings Preloop holds, most specific first."""

    def test_alias_precedes_identifier_and_nothing_repeats(self):
        candidates = catalog_candidates(
            "acme-small", provider_name="Acme", model_alias="acme/acme-small"
        )
        assert candidates[0] == "acme/acme-small"
        assert "acme-small" in candidates
        assert "acme.acme-small" in candidates
        assert len(candidates) == len(set(candidates))

    def test_no_model_means_no_candidates(self):
        assert catalog_candidates(None, provider_name="acme") == []


class TestExecutionContext:
    """What the harnesses actually call."""

    def test_reads_the_keys_the_execution_context_carries(self):
        limits = limits_for_execution(
            {
                "model_identifier": "acme-large",
                "model_provider": "acme",
                "model_parameters": {"max_output_tokens": 4096},
            }
        )
        assert limits.context_window == 900000
        assert limits.max_output_tokens == 4096

    def test_an_empty_context_resolves_to_nothing(self):
        assert limits_for_execution({}).known is False

    def test_a_broken_lookup_never_blocks_a_run(self, monkeypatch, caplog):
        """A harness must still start when the catalog cannot be read."""

        def explode(**_kwargs):
            raise RuntimeError("catalog on fire")

        monkeypatch.setattr(limits_module, "resolve_model_context_limits", explode)
        with caplog.at_level(logging.ERROR):
            limits = limits_for_execution({"model_identifier": "acme-large"})
        assert limits == ModelContextLimits()


class TestVendoredCatalog:
    """One check against the real snapshot, which is release-pinned."""

    def test_the_real_catalog_carries_the_fields_this_reads(self, monkeypatch):
        monkeypatch.undo()
        limits_module._catalog.cache_clear()
        limits = resolve_model_context_limits(model_identifier="gpt-4o")
        assert limits.context_window == 128000
        assert limits.max_output_tokens == 16384
        assert limits.context_window_source == "catalog"
