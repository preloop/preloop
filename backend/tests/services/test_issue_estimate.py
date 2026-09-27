"""Reading the human estimate of an issue from its tracker."""

from __future__ import annotations

from decimal import Decimal

import pytest

from preloop.services.issue_estimate import (
    EstimateConfig,
    estimate_config,
    label_names,
    read_estimate,
    synced_estimate_fields,
)

NO_CONFIG = EstimateConfig()


def test_jira_original_estimate_seconds_become_hours() -> None:
    estimate = read_estimate(
        tracker_type="jira",
        fields={"timeoriginalestimate": 27000},
        labels=[],
        config=NO_CONFIG,
    )
    assert estimate.hours == Decimal("7.50")
    assert estimate.hours_source == "jira:timeoriginalestimate"
    assert estimate.points is None and estimate.points_source is None


def test_jira_timetracking_form_is_read_too() -> None:
    estimate = read_estimate(
        tracker_type="jira",
        fields={"timetracking": {"originalEstimateSeconds": 3600}},
        labels=[],
        config=NO_CONFIG,
    )
    assert estimate.hours == Decimal("1.00")


def test_jira_story_points_come_from_the_configured_field() -> None:
    config = estimate_config({"points_field": "customfield_10016"})
    estimate = read_estimate(
        tracker_type="jira",
        fields={"customfield_10016": 5.0, "customfield_10002": 8},
        labels=[],
        config=config,
    )
    assert estimate.points == Decimal("5.00")
    assert estimate.points_source == "jira:customfield_10016"
    assert estimate.hours is None


def test_gitlab_time_estimate_and_weight() -> None:
    config = estimate_config({"points_field": "weight"})
    estimate = read_estimate(
        tracker_type="gitlab",
        fields={"time_stats": {"time_estimate": 5400}, "weight": 3},
        labels=[],
        config=config,
    )
    assert estimate.hours == Decimal("1.50")
    assert estimate.hours_source == "gitlab:time_estimate"
    assert estimate.points == Decimal("3.00")
    assert estimate.points_source == "gitlab:weight"


def test_github_labels_with_configured_prefixes() -> None:
    config = estimate_config(
        {"hours_label_prefix": "estimate:", "points_label_prefix": "points:"}
    )
    estimate = read_estimate(
        tracker_type="github",
        fields=None,
        labels=label_names([{"name": "Estimate: 4h"}, {"name": "points:3"}]),
        config=config,
    )
    assert estimate.hours == Decimal("4.00")
    assert estimate.hours_source == "label:estimate:"
    assert estimate.points == Decimal("3.00")
    assert estimate.points_source == "label:points:"


def test_nothing_is_invented() -> None:
    # No estimate fields and no configuration: empty, not zero.
    estimate = read_estimate(
        tracker_type="github",
        fields={"title": "x", "number": 12},
        labels=["estimate:4h", "bug"],
        config=NO_CONFIG,
    )
    assert estimate.empty
    # A zero Jira estimate means "not estimated".
    assert read_estimate(
        tracker_type="jira",
        fields={"timeoriginalestimate": 0},
        labels=[],
        config=NO_CONFIG,
    ).empty
    # Native fields of another tracker type are not read.
    assert read_estimate(
        tracker_type="github",
        fields={"timeoriginalestimate": 3600, "time_estimate": 3600},
        labels=[],
        config=NO_CONFIG,
    ).empty


def test_conflicting_labels_are_not_an_estimate() -> None:
    config = estimate_config({"points_label_prefix": "sp:"})
    estimate = read_estimate(
        tracker_type="github",
        fields=None,
        labels=["sp:3", "sp:5"],
        config=config,
    )
    assert estimate.points is None
    # The same value twice is fine.
    assert read_estimate(
        tracker_type="github", fields=None, labels=["sp:3", "SP:3"], config=config
    ).points == Decimal("3.00")


@pytest.mark.parametrize(
    "value", ["-1", "NaN", "Infinity", "1e12", True, "three", {"value": None}]
)
def test_bad_points_values_are_ignored(value: object) -> None:
    config = estimate_config({"points_field": "sp"})
    assert (
        read_estimate(
            tracker_type="jira", fields={"sp": value}, labels=[], config=config
        ).points
        is None
    )


def test_bad_configuration_is_ignored() -> None:
    assert estimate_config("points_field") == NO_CONFIG
    assert estimate_config({"points_field": "a b; drop"}).points_field is None
    assert estimate_config({"points_field": 7}).points_field is None
    assert estimate_config({"hours_label_prefix": "  "}).hours_label_prefix is None


def test_synced_estimate_fields_keeps_only_raw_native_values() -> None:
    assert synced_estimate_fields(
        {"fields": {"timeoriginalestimate": 7200, "summary": "x"}}
    ) == {"timeoriginalestimate": 7200}
    assert synced_estimate_fields(
        {"time_stats": {"time_estimate": 600}, "weight": 2}
    ) == {"time_estimate": 600, "weight": 2}
    assert synced_estimate_fields({"weight": True, "time_estimate": None}) == {}
    assert synced_estimate_fields(None) == {}


def test_label_names_accepts_every_payload_shape() -> None:
    assert label_names(["a", {"name": "b"}, {"title": "c"}, {"id": 1}, "  "]) == [
        "a",
        "b",
        "c",
    ]
    assert label_names("a,b") == []
