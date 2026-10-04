"""CEL vs simple detection must match the access-rule create/update path."""

from preloop.services.policy.loader import _detect_condition_type
from preloop.services.policy_evaluator import is_simple_expression


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


def test_is_simple_expression_accepts_simple_grammar() -> None:
    """The model-rule simple parser reads comparisons and its own methods."""
    assert is_simple_expression("pii.found == true")
    assert is_simple_expression("injection.score > 0.7")
    assert is_simple_expression("")
    # contains()/matches() are part of the bindings simple evaluator.
    assert is_simple_expression('pii.types_found.contains("email")')
    assert is_simple_expression('model.id.matches("gpt-.*")')


def test_is_simple_expression_rejects_cel_only_syntax() -> None:
    assert not is_simple_expression("'credit_card' in pii.types_found")
    assert not is_simple_expression("pii.found && size(pii.types_found) > 1")
    assert not is_simple_expression("!pii.found")
    assert not is_simple_expression("pii.types_found[0] == 'email'")
