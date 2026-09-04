"""CEL vs simple detection must match the access-rule create/update path."""

from preloop.services.policy.loader import _detect_condition_type


def test_simple_comparisons_stay_simple() -> None:
    assert _detect_condition_type("moderation.flagged == true") == "simple"
    assert _detect_condition_type("injection.score > 0.7") == "simple"
    assert _detect_condition_type("pii.found != true") == "simple"
    assert _detect_condition_type("") == "simple"


def test_cel_operators_and_functions_are_cel() -> None:
    assert _detect_condition_type("!args.enabled") == "cel"
    assert _detect_condition_type("args.priority in ['critical','high']") == "cel"
    assert _detect_condition_type("args.ok ? true : false") == "cel"
    assert _detect_condition_type('args.name.contains("x")') == "cel"
    assert _detect_condition_type("a && b") == "cel"
    assert _detect_condition_type("items[0]") == "cel"
