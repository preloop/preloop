"""Dependency injection hooks for the API."""

from preloop.services.model_gateway_budget_enforcer import ModelGatewayBudgetEnforcer


def get_budget_enforcer() -> ModelGatewayBudgetEnforcer:
    """Enforce basic budgets, with optional enterprise dependency overrides."""
    return ModelGatewayBudgetEnforcer()
