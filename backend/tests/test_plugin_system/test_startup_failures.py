"""Plugin failures must stop a configured process before it starts work."""

import importlib
import logging
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from preloop.plugins import base
from preloop.services.policy import loader
from preloop.sync.cli.commands import cli


@pytest.mark.parametrize(
    "arguments",
    [
        ["worker"],
        ["worker", "--tasks", "poll_tracker"],
        ["worker", "--exclude-tasks", "execute_flow"],
        ["scheduler"],
        ["monitor"],
    ],
)
def test_entrypoints_load_plugins_before_service_loop(arguments: list[str]) -> None:
    """A broken configured plugin prevents every worker argument shape starting."""
    with (
        patch.object(
            base, "get_plugin_manager", side_effect=RuntimeError("broken plugin")
        ),
        patch(
            "preloop.sync.cli.worker_commands.main",
            side_effect=RuntimeError("loop reached"),
        ),
        patch(
            "preloop.sync.cli.monitor_commands.main",
            side_effect=RuntimeError("loop reached"),
        ),
        patch(
            "preloop.sync.cli.scheduler_commands.get_db_session",
            side_effect=RuntimeError("loop reached"),
        ),
    ):
        result = CliRunner().invoke(cli, arguments)
    assert result.exit_code != 0
    assert "broken plugin" in str(result.exception)


def test_required_discovery_is_fatal(caplog: pytest.LogCaptureFixture) -> None:
    """Discovery logs the package and cause and propagates the failure."""
    with caplog.at_level(logging.ERROR), pytest.raises(ImportError):
        base.PluginManager().discover_plugins("missing_enabled_plugin_package")
    assert "missing_enabled_plugin_package" in caplog.text


def test_registration_is_atomic() -> None:
    """Failure in component discovery must not publish a partly loaded plugin."""
    manager = base.PluginManager()
    plugin = Mock()
    plugin.metadata.name = "example"
    plugin.get_services.return_value = {"example_service": object()}
    plugin.get_condition_evaluators.side_effect = RuntimeError("invalid settings")
    with pytest.raises(RuntimeError):
        manager.register_plugin(plugin)
    assert "example" not in manager._plugins
    assert manager.get_service("example_service") is None


def test_nested_import_failure_is_not_an_absent_optional_package() -> None:
    """An installed package missing a dependency must not look like OSS."""
    from preloop.config import settings

    real_import = importlib.import_module

    def import_package(name: str) -> ModuleType:
        if name == "preloop.plugins.proprietary":
            raise ModuleNotFoundError("missing dependency", name="example_dependency")
        return real_import(name)

    base.reset_plugin_manager()
    try:
        with (
            patch.object(settings, "disable_rbac", False),
            patch.dict("os.environ", {"DISABLE_PROPRIETARY_PLUGINS": "false"}),
            patch("importlib.import_module", side_effect=import_package),
        ):
            with pytest.raises(ImportError):
                base.get_plugin_manager()
            assert base._plugin_manager is None
    finally:
        base.reset_plugin_manager()


def test_cel_validation_resolves_registered_service() -> None:
    """Validation works without a top-level plugins package on sys.path."""
    service = object()
    manager = SimpleNamespace(get_service=Mock(return_value=service))
    with patch.object(base, "get_plugin_manager", return_value=manager):
        assert loader._get_cel_validation_service() is service
    manager.get_service.assert_called_once_with("cel_validation_service")


def test_registered_cel_service_rejects_invalid_tool_and_model_policy() -> None:
    """Both tool and model I/O CEL checks run via the installed service."""
    service = Mock()
    service.validate_cel_expression.return_value = (False, "invalid expression")
    manager = SimpleNamespace(get_service=Mock(return_value=service))
    condition = SimpleNamespace(condition_type="cel", expression="args.value &&")
    policy = SimpleNamespace(
        tools=[SimpleNamespace(conditions=[condition])],
        model_io=[SimpleNamespace(conditions=[condition])],
    )
    with patch.object(base, "get_plugin_manager", return_value=manager):
        errors = loader._validate_cel_expressions_in_policy(policy)
    assert len(errors) == 2
    assert {error.path for error in errors} == {
        "$.tools[0].conditions[0].expression",
        "$.model_io[0].conditions[0].expression",
    }


def test_installed_plugin_registration_failure_is_fatal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Configuration-dependent registration failures name the failing plugin."""
    package = tmp_path / "example_plugins"
    (package / "broken").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "broken" / "__init__.py").write_text(
        "def register(manager: object) -> None:\n    raise RuntimeError('invalid plugin configuration')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        base.PluginManager().discover_plugins("example_plugins")
    assert "example_plugins.broken" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "invalid plugin configuration" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("disabled_by", ["rbac", "proprietary"])
def test_disabled_plugins_never_import_proprietary_package(disabled_by: str) -> None:
    """Both existing flags prevent all proprietary imports."""
    from preloop.config import settings

    real_import = importlib.import_module
    imports = []

    def record_import(name: str) -> ModuleType:
        imports.append(name)
        return real_import(name)

    base.reset_plugin_manager()
    try:
        with (
            patch.object(settings, "disable_rbac", disabled_by == "rbac"),
            patch.dict(
                "os.environ",
                {"DISABLE_PROPRIETARY_PLUGINS": str(disabled_by == "proprietary")},
            ),
            patch("importlib.import_module", side_effect=record_import),
        ):
            manager = base.get_plugin_manager()
        assert not manager.proprietary_enabled
        assert "preloop.plugins.proprietary" not in imports
    finally:
        base.reset_plugin_manager()
