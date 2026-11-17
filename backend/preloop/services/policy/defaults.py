"""Account policy defaults, shared by export and strict enforcement readers."""

from typing import Any

from preloop.services.policy.schema import DefaultsDefinition

POLICY_DEFAULTS_META_KEY = "policy_defaults"


def parse_defaults(raw: Any) -> DefaultsDefinition:
    """Read defaults without changing accounts that have never opted in.

    Missing blocks use the historical allow/false defaults. Malformed stored
    blocks raise validation errors; enforcement must never treat them as allow.
    """
    return DefaultsDefinition.model_validate({} if raw is None else raw)
