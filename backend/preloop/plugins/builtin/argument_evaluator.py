"""Built-in ArgumentEvaluator plugin for CEL-based conditional approval rules.

This plugin provides the core open-source functionality for evaluating
conditional approval rules based on tool arguments using Google's Common
Expression Language (CEL).
"""

import logging
from typing import Any, Dict, List, Optional

import celpy

from preloop.plugins.base import (
    ConditionEvaluatorPlugin,
    Plugin,
    PluginMetadata,
)

logger = logging.getLogger(__name__)


class ArgumentEvaluator(ConditionEvaluatorPlugin):
    """Built-in evaluator for argument-based conditions using CEL.

    This is the core open source functionality (Phase 1).
    Evaluates CEL expressions against tool arguments to determine if
    approval is required.

    Example expressions:
        - args.amount > 1000
        - args.priority == 'critical'
        - args.amount > 1000 && args.currency == 'USD'
        - 'urgent' in args.labels
    """

    @property
    def metadata(self) -> PluginMetadata:
        """Return evaluator metadata."""
        return PluginMetadata(
            name="Argument Evaluator",
            version="1.0.0",
            author="Preloop Team",
            description="Evaluate conditions based on tool arguments using CEL",
        )

    @property
    def condition_type(self) -> str:
        """Return condition type identifier."""
        return "argument"

    async def evaluate(
        self,
        condition_config: Dict[str, Any],
        tool_args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Evaluate CEL expression against tool arguments.

        Args:
            condition_config: Configuration containing 'expression' key
            tool_args: Arguments passed to the tool
            context: Additional context (unused for argument evaluation)

        Returns:
            True if condition matches (approval required), False otherwise

        Raises:
            ValueError: If expression is missing or invalid
        """
        expression = condition_config.get("expression")
        if not expression:
            raise ValueError("Missing 'expression' in condition_config")

        try:
            # Create CEL environment
            env = celpy.Environment()
            ast = env.compile(expression)
            program = env.program(ast)

            # Evaluate with tool arguments (convert to CEL types)
            activation = celpy.json_to_cel({"args": tool_args})
            result = program.evaluate(activation)

            return bool(result)

        except celpy.CELParseError as e:
            logger.error("Condition evaluation failed: error_type=%s", type(e).__name__)
            raise ValueError(f"Invalid CEL syntax: {str(e)}")
        except celpy.CELEvalError as e:
            logger.error("Condition evaluation failed: error_type=%s", type(e).__name__)
            raise ValueError(f"CEL evaluation failed: {str(e)}")
        except Exception as e:
            logger.error("Condition evaluation failed: error_type=%s", type(e).__name__)
            raise

    async def validate_config(self, condition_config: Dict[str, Any]) -> List[str]:
        """Validate CEL expression configuration.

        Args:
            condition_config: Configuration to validate

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        if "expression" not in condition_config:
            errors.append("Missing required field: 'expression'")
            return errors

        expression = condition_config["expression"]

        # Validate expression is a string
        if not isinstance(expression, str):
            errors.append("Expression must be a string")
            return errors

        # Validate expression length
        if len(expression) > 500:
            errors.append("Expression too long (max 500 characters)")

        # Try to compile CEL expression
        try:
            env = celpy.Environment()
            env.compile(expression)
        except celpy.CELParseError as e:
            errors.append(f"Invalid CEL syntax: {str(e)}")
        except Exception as e:
            errors.append(f"Error validating expression: {str(e)}")

        return errors

    def get_schema(self) -> Dict[str, Any]:
        """Return JSON schema for UI validation."""
        return {
            "type": "object",
            "required": ["expression"],
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "CEL expression to evaluate against tool arguments",
                    "maxLength": 500,
                    "examples": [
                        "args.amount > 1000",
                        "args.priority == 'critical'",
                        "'urgent' in args.labels",
                        "args.amount > 1000 && args.currency == 'USD'",
                    ],
                }
            },
        }

    def get_examples(self) -> List[Dict[str, Any]]:
        """Return example configurations."""
        return [
            {
                "name": "High-value transactions",
                "description": "Require approval for transactions over $1000",
                "config": {"expression": "args.amount > 1000"},
                "sample_args": {"amount": 1500},
                "expected_result": True,
            },
            {
                "name": "Production deployments",
                "description": "Require approval for production environment",
                "config": {"expression": "args.environment == 'production'"},
                "sample_args": {"environment": "production"},
                "expected_result": True,
            },
            {
                "name": "Critical issues",
                "description": "Require approval for critical priority issues",
                "config": {
                    "expression": "args.priority == 'critical' && args.severity >= 8"
                },
                "sample_args": {"priority": "critical", "severity": 9},
                "expected_result": True,
            },
            {
                "name": "Urgent labels",
                "description": "Require approval for issues with urgent label",
                "config": {"expression": "'urgent' in args.labels"},
                "sample_args": {"labels": ["urgent", "bug"]},
                "expected_result": True,
            },
            {
                "name": "High-value USD transactions",
                "description": "Require approval for USD transactions over $1000",
                "config": {
                    "expression": "args.amount > 1000 && args.currency == 'USD'"
                },
                "sample_args": {"amount": 1500, "currency": "USD"},
                "expected_result": True,
            },
        ]


class BuiltinPlugin(Plugin):
    """Built-in plugin providing core open-source evaluators."""

    @property
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="builtin",
            version="1.0.0",
            author="Preloop Team",
            description="Built-in evaluators and features",
        )

    def get_condition_evaluators(self) -> List[ConditionEvaluatorPlugin]:
        """Return list of condition evaluators."""
        return [ArgumentEvaluator()]


def register(plugin_manager):
    """Register built-in plugin with the plugin manager.

    This function is called by the plugin manager during discovery.

    Args:
        plugin_manager: The PluginManager instance
    """
    plugin_manager.register_plugin(BuiltinPlugin())
    logger.info("Registered built-in plugin with ArgumentEvaluator")
