"""Base classes for Preloop plugin system.

This module provides the core plugin architecture for extending Preloop
functionality. Plugins can provide services, API routes, background tasks,
condition evaluators, and workflow orchestrators.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)


@dataclass
class PluginMetadata:
    """Metadata for a plugin."""

    name: str
    version: str
    author: str
    description: str


@dataclass
class RouterConfig:
    """Configuration for a plugin router.

    Attributes:
        router: The FastAPI APIRouter instance
        prefix: URL prefix (e.g., "/api/v1")
        tags: OpenAPI tags for the router
        dependencies: FastAPI dependencies to apply to all routes
        include_in_schema: Whether to include in OpenAPI schema
        require_auth: If True, adds get_current_active_user dependency
    """

    router: APIRouter
    prefix: str = "/api/v1"
    tags: Optional[List[str]] = None
    dependencies: Optional[List[Any]] = None
    include_in_schema: bool = True
    require_auth: bool = True  # Most plugin routes require authentication


class Plugin(ABC):
    """Base class for all Preloop plugins.

    Plugins can provide:
    - Services (business logic, integrations)
    - API routes (additional endpoints, webhooks)
    - Background tasks (scheduled jobs, workers)
    - Condition evaluators (for approval rules)
    - Workflow orchestrators (for complex approvals)
    """

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        pass

    def get_routers(self) -> List[RouterConfig]:
        """Return list of RouterConfig objects to register.

        Plugins should override this to provide their API routes.
        Each RouterConfig specifies the router, prefix, tags, and dependencies.

        For backward compatibility, can also return List[tuple[APIRouter, str]]
        which will be converted to RouterConfig with default settings.

        Example:
            return [
                RouterConfig(
                    router=my_router,
                    prefix="/api/v1",
                    tags=["MyPlugin"],
                    require_auth=True,
                ),
            ]
        """
        return []

    def get_services(self) -> Dict[str, Any]:
        """Return services to register (name -> instance)."""
        return {}

    def get_dependencies(self) -> Dict[Any, Any]:
        """Return FastAPI dependencies to override."""
        return {}

    def get_features(self) -> Dict[str, bool]:
        """Return feature flags this plugin enables.

        Plugins should override this to declare which features they provide.
        These are exposed via the /api/v1/features endpoint for frontend use.

        Returns:
            Dictionary of feature_name -> enabled (True)

        Example:
            return {
                "audit_logs": True,
                "advanced_reporting": True,
            }
        """
        return {}

    def get_tasks(self) -> List[Dict[str, Any]]:
        """Return background tasks to register."""
        return []

    def get_condition_evaluators(self) -> List["ConditionEvaluatorPlugin"]:
        """Return condition evaluators for approval rules."""
        return []

    def get_workflow_orchestrators(self) -> List["WorkflowOrchestratorPlugin"]:
        """Return workflow orchestrators for complex approvals."""
        return []

    async def on_startup(self):
        """Called when application starts."""
        return

    async def on_shutdown(self):
        """Called when application shuts down."""
        return


class ConditionEvaluatorPlugin(ABC):
    """Abstract base for condition evaluator plugins."""

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return evaluator metadata."""
        pass

    @property
    @abstractmethod
    def condition_type(self) -> str:
        """Unique identifier for this condition type (e.g., 'argument', 'team', 'external')."""
        pass

    @abstractmethod
    async def evaluate(
        self,
        condition_config: Dict[str, Any],
        tool_args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Evaluate condition and return True if approval is required.

        Args:
            condition_config: Configuration from ApprovalRule.condition_config
            tool_args: Arguments passed to the tool
            context: Additional context (account_id, user_id, etc.)

        Returns:
            True if condition matches (approval required), False otherwise
        """
        pass

    @abstractmethod
    async def validate_config(self, condition_config: Dict[str, Any]) -> List[str]:
        """Validate condition configuration.

        Returns:
            List of error messages (empty if valid)
        """
        pass

    async def test(
        self, condition_config: Dict[str, Any], tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Test condition with sample data (returns debug info)."""
        try:
            result = await self.evaluate(condition_config, tool_args)
            return {
                "result": result,
                "trace": [f"Evaluation result: {result}"],
                "errors": [],
            }
        except Exception as e:
            return {"result": False, "trace": [], "errors": [str(e)]}

    def get_schema(self) -> Dict[str, Any]:
        """Return JSON schema for condition_config (for UI validation)."""
        return {}

    def get_examples(self) -> List[Dict[str, Any]]:
        """Return example configurations."""
        return []


class WorkflowOrchestratorPlugin(ABC):
    """Abstract base for approval workflow orchestrators (Phase 3)."""

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return orchestrator metadata."""
        pass

    @property
    @abstractmethod
    def workflow_type(self) -> str:
        """Unique identifier for this workflow type (e.g., 'escalation', 'quorum')."""
        pass

    @abstractmethod
    async def execute_workflow(
        self,
        approval_request_id: str,
        workflow_config: Dict[str, Any],
        policy_config: Dict[str, Any],
    ) -> str:
        """Execute approval workflow.

        Returns:
            Final approval status: "approved", "declined", "expired"
        """
        pass

    @abstractmethod
    async def get_status(self, approval_request_id: str) -> Dict[str, Any]:
        """Get current workflow status."""
        pass


class PluginManager:
    """Manages plugin lifecycle and registration."""

    def __init__(self):
        """Initialize plugin manager."""
        self._plugins: Dict[str, Plugin] = {}
        self._services: Dict[str, Any] = {}
        self._condition_evaluators: Dict[str, ConditionEvaluatorPlugin] = {}
        self._workflow_orchestrators: Dict[str, WorkflowOrchestratorPlugin] = {}
        self._discovered_modules: set = (
            set()
        )  # Track discovered modules to prevent infinite recursion

    def register_plugin(self, plugin: Plugin):
        """Register a plugin and all its components."""
        if plugin.metadata.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.metadata.name}' already registered")

        self._plugins[plugin.metadata.name] = plugin

        # Register services
        for service_name, service in plugin.get_services().items():
            self._services[service_name] = service

        # Register condition evaluators
        for evaluator in plugin.get_condition_evaluators():
            self._condition_evaluators[evaluator.condition_type] = evaluator

        # Register workflow orchestrators
        for orchestrator in plugin.get_workflow_orchestrators():
            self._workflow_orchestrators[orchestrator.workflow_type] = orchestrator

        logger.info(
            f"Registered plugin: {plugin.metadata.name} v{plugin.metadata.version}"
        )

    def get_service(self, name: str) -> Any:
        """Get registered service by name."""
        return self._services.get(name)

    def get_condition_evaluator(
        self, condition_type: str
    ) -> Optional[ConditionEvaluatorPlugin]:
        """Get condition evaluator by type."""
        return self._condition_evaluators.get(condition_type)

    def get_workflow_orchestrator(
        self, workflow_type: str
    ) -> Optional[WorkflowOrchestratorPlugin]:
        """Get workflow orchestrator by type."""
        return self._workflow_orchestrators.get(workflow_type)

    def list_condition_evaluators(self) -> List[str]:
        """List all registered condition evaluator types."""
        return list(self._condition_evaluators.keys())

    def list_workflow_orchestrators(self) -> List[str]:
        """List all registered workflow orchestrator types."""
        return list(self._workflow_orchestrators.keys())

    def get_enabled_features(self) -> Dict[str, Any]:
        """Get list of enabled features/plugins for frontend consumption.

        Note: The 'builtin' plugin is excluded from the plugins list since it
        only provides internal functionality (argument evaluator) and is not
        a user-facing enterprise plugin.
        """
        features = {"plugins": [], "features": {}}

        for _plugin_name, plugin in self._plugins.items():
            # Skip builtin plugin from the list - it's not an enterprise plugin
            # and only provides internal functionality (argument evaluator)
            if plugin.metadata.name == "builtin":
                continue

            features["plugins"].append(
                {
                    "name": plugin.metadata.name,
                    "version": plugin.metadata.version,
                    "description": plugin.metadata.description,
                }
            )

            # Let each plugin declare its own features
            plugin_features = plugin.get_features()
            for feature_name, value in plugin_features.items():
                if value is True or value is False:
                    if value:
                        features["features"][feature_name] = True
                elif value:
                    # Preserve non-boolean values (e.g. lists) as-is
                    features["features"][feature_name] = value

        return features

    async def startup_all(self):
        """Call on_startup for all plugins."""
        for plugin in self._plugins.values():
            try:
                await plugin.on_startup()
            except Exception as e:
                logger.error(
                    f"Error starting plugin '{plugin.metadata.name}': {e}",
                    exc_info=True,
                )

    async def shutdown_all(self):
        """Call on_shutdown for all plugins."""
        for plugin in self._plugins.values():
            try:
                await plugin.on_shutdown()
            except Exception as e:
                logger.error(
                    f"Error shutting down plugin '{plugin.metadata.name}': {e}",
                    exc_info=True,
                )

    def register_routes(self, app):
        """Register all plugin routes with FastAPI app."""
        from fastapi import Depends
        from preloop.api.auth import get_current_active_user

        for plugin in self._plugins.values():
            for item in plugin.get_routers():
                # Support both old tuple format and new RouterConfig
                if isinstance(item, RouterConfig):
                    config = item
                elif isinstance(item, tuple) and len(item) == 2:
                    # Backward compatibility: (router, prefix) tuple
                    router, prefix = item
                    config = RouterConfig(router=router, prefix=prefix)
                else:
                    logger.warning(
                        f"Invalid router config from plugin '{plugin.metadata.name}': {item}"
                    )
                    continue

                # Build dependencies list
                dependencies = list(config.dependencies or [])
                if config.require_auth:
                    dependencies.append(Depends(get_current_active_user))

                # Register the router
                app.include_router(
                    config.router,
                    prefix=config.prefix,
                    tags=config.tags or [],
                    dependencies=dependencies if dependencies else None,
                    include_in_schema=config.include_in_schema,
                )
                logger.info(
                    f"Registered routes from plugin '{plugin.metadata.name}' at {config.prefix}"
                )

    def discover_plugins(self, plugin_module: str = "plugins"):
        """Discover and load plugins from a Python package.

        Args:
            plugin_module: Dotted module path (e.g. "preloop.plugins.builtin")
        """
        import importlib
        import pkgutil

        # Prevent infinite recursion
        if plugin_module in self._discovered_modules:
            return
        self._discovered_modules.add(plugin_module)

        try:
            # Import the plugin package
            package = importlib.import_module(plugin_module)

            # Get the package's path for pkgutil
            if not hasattr(package, "__path__"):
                logger.warning(f"Plugin module '{plugin_module}' is not a package")
                return

            # Iterate through submodules and sub-packages
            for _finder, name, ispkg in pkgutil.iter_modules(package.__path__):
                full_module_name = f"{plugin_module}.{name}"

                # Skip pytest configuration files and test directories
                if name == "conftest" or name == "tests" or name.startswith("test_"):
                    continue

                # Skip if already discovered
                if full_module_name in self._discovered_modules:
                    continue

                try:
                    # Import the submodule or sub-package
                    module = importlib.import_module(full_module_name)
                    # Check if it has a register function
                    if hasattr(module, "register"):
                        module.register(self)
                        logger.info(f"Loaded plugin module: {full_module_name}")
                    elif ispkg:
                        # If it's a package without a register function, try to discover plugins within it
                        self.discover_plugins(full_module_name)
                except Exception as e:
                    logger.error(
                        f"Failed to load plugin '{full_module_name}': {e}",
                        exc_info=True,
                    )
        except ImportError as e:
            logger.warning(f"Plugin module not found: {plugin_module} - {e}")


# Global singleton
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """Get global plugin manager (singleton)."""
    import os

    from preloop.config import settings

    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
        _plugin_manager.discover_plugins("preloop.plugins.builtin")

        # Only load proprietary plugins if not explicitly disabled
        # Check both disable_rbac (for permission checks) and DISABLE_PROPRIETARY_PLUGINS (for plugin loading)
        disable_proprietary = (
            os.getenv("DISABLE_PROPRIETARY_PLUGINS", "false").lower() == "true"
        )

        if settings.disable_rbac:
            logger.info("DISABLE_RBAC is set - skipping proprietary plugins")
        elif disable_proprietary:
            logger.info(
                "DISABLE_PROPRIETARY_PLUGINS is set - skipping proprietary plugins"
            )
        else:
            logger.info("Loading proprietary plugins from preloop.plugins.proprietary")
            _plugin_manager.discover_plugins("preloop.plugins.proprietary")

        # Log loaded plugins summary
        plugin_names = list(_plugin_manager._plugins.keys())
        logger.info(f"Plugin manager initialized with plugins: {plugin_names}")
    return _plugin_manager


def reset_plugin_manager():
    """Reset global plugin manager (for testing)."""
    global _plugin_manager
    _plugin_manager = None
