"""Shared internal-purpose vocabulary and indexable agent-traffic predicate."""

GATEWAY_USAGE_PURPOSES = frozenset(
    {
        "session_title",
        "session_optimization",
        "replay_validation",
        "session_embedding",
        "governance_session_digest",
        "governance_review",
    }
)

# Keep the migration snapshot in sync when extending the indexed vocabulary.
AGENT_GATEWAY_PREDICATE = (
    "action_type = 'model_gateway' AND ((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') NOT IN ("
    + ", ".join(repr(purpose) for purpose in sorted(GATEWAY_USAGE_PURPOSES))
    + "))"
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
