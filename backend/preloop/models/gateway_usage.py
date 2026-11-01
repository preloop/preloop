"""Shared internal-purpose vocabulary and indexable agent-traffic predicate."""

GATEWAY_USAGE_PURPOSES = frozenset(
    {
        "session_title",
        "session_optimization",
        "replay_validation",
        "session_embedding",
        "governance_session_digest",
        "governance_review",
        "governance_exception_synthesis",
    }
)

# New exclusions narrow the live query. The published partial index remains
# a safe superset, avoiding a large index rebuild for a two-call weekly purpose.
AGENT_GATEWAY_PREDICATE = (
    "action_type = 'model_gateway' AND ((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') NOT IN ("
    + ", ".join(repr(purpose) for purpose in sorted(GATEWAY_USAGE_PURPOSES))
    + "))"
)

# Frozen to the published 20261017 candidate index. Do not change without an
# explicit index migration. Live queries additionally exclude synthesis.
AGENT_GATEWAY_INDEX_PREDICATE = AGENT_GATEWAY_PREDICATE.replace(
    "'governance_exception_synthesis', ", ""
)

# Customer reports keep other purpose semantics, but exclude governance analysis
# regardless of funding. Literal spellings also match generic prepared plans.
CUSTOMER_GATEWAY_PREDICATE = (
    "action_type = 'model_gateway' AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') <> 'replay_validation') AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "left(meta_data ->> 'purpose', 11) <> 'governance_')"
)
