"""Native control sessions must preserve governance and report real outcomes."""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from preloop_hermes_plugin.runtime import NativeHermesRuntime  # noqa: E402
from preloop.integrations.agent_control import OperatorCommand  # noqa: E402


@pytest.fixture
def native(monkeypatch):
    agent = Mock()
    agent.tools = []
    agent.run_conversation.return_value = {"final_response": "example result"}
    factory = Mock(return_value=agent)
    database = Mock()
    modules = {
        "hermes_cli.plugins": SimpleNamespace(
            PluginContext=lambda manifest, manager: manager,
            PluginManifest=lambda **kw: kw,
            discover_plugins=Mock(),
            get_plugin_manager=lambda: "registry",
        ),
        "hermes_cli.env_loader": SimpleNamespace(load_hermes_dotenv=Mock()),
        "hermes_cli.config": SimpleNamespace(
            load_config=lambda: {"model": {"default": "example-model"}}
        ),
        "hermes_cli.runtime_provider": SimpleNamespace(
            resolve_runtime_provider=lambda **kw: {"provider": "example"}
        ),
        "hermes_cli.tools_config": SimpleNamespace(
            _get_platform_tools=lambda *args: {"terminal"}
        ),
        "hermes_state": SimpleNamespace(SessionDB=lambda: database),
        "run_agent": SimpleNamespace(AIAgent=factory),
    }
    for key, value in modules.items():
        monkeypatch.setitem(sys.modules, key, value)
    plugin = Mock()
    runtime = NativeHermesRuntime(plugin)
    plugin.verify.assert_called_once()
    plugin.register.assert_called_once_with("registry")
    return runtime, agent, factory, database


@pytest.mark.asyncio
async def test_native_fresh_sessions_return_native_results_and_close(native):
    runtime, agent, factory, database = native
    result = await runtime.send_command(
        OperatorCommand(command_id="example", text="hello", session_mode="new")
    )
    assert result.reply_text == "example result"
    assert result.session_reference == factory.call_args.kwargs["session_id"]
    agent.run_conversation.assert_called_once_with("hello")
    agent.close.assert_called_once()
    database.close.assert_called_once()
    caps = runtime.capabilities()
    assert caps.supports_new_session and caps.supports_tool_approval
    assert not caps.supports_existing_session
    assert not caps.supports_interrupt and not caps.supports_voice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [{"failed": True}, {"partial": True}, {"final_response": ""}]
)
async def test_native_failure_is_terminal_and_closes_resources(native, result):
    runtime, agent, _, database = native
    agent.run_conversation.return_value = result
    with pytest.raises(RuntimeError):
        await runtime.send_command(
            OperatorCommand(command_id="example", text="hello", session_mode="new")
        )
    agent.close.assert_called_once()
    database.close.assert_called_once()


@pytest.mark.asyncio
async def test_native_rejects_existing_session_without_running(native):
    runtime, agent, _, _ = native
    with pytest.raises(ValueError, match="new session"):
        await runtime.send_command(
            OperatorCommand(command_id="example", text="hello", session_mode="existing")
        )
    agent.run_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_native_explicit_budget_rejected_before_model_call(native):
    runtime, agent, _, _ = native
    with pytest.raises(ValueError, match="cost/token"):
        await runtime.send_command(
            OperatorCommand(
                command_id="example",
                text="hello",
                session_mode="new",
                metadata={"run_limits": {"max_usd": 1}},
            )
        )
    agent.run_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_native_iteration_limit_and_tools_disabled(native):
    runtime, _, factory, _ = native
    await runtime.send_command(
        OperatorCommand(
            command_id="example",
            text="hello",
            session_mode="new",
            metadata={"run_limits": {"max_turns": 8}},
        )
    )
    assert factory.call_args.kwargs["max_iterations"] == 8
    assert factory.call_args.kwargs["enabled_toolsets"] == []


@pytest.mark.asyncio
async def test_native_deadline_requests_interrupt_and_waits_for_result(
    native, monkeypatch
):
    runtime, agent, _, database = native
    timer = Mock()

    def create_timer(duration, callback):
        timer.start.side_effect = callback
        return timer

    monkeypatch.setattr("preloop_hermes_plugin.runtime.threading.Timer", create_timer)
    with pytest.raises(TimeoutError):
        await runtime.send_command(
            OperatorCommand(
                command_id="example",
                text="hello",
                session_mode="new",
                metadata={"timeout_seconds": 1},
            )
        )
    agent.interrupt.assert_called_once()
    agent.run_conversation.assert_called_once()
    timer.cancel.assert_called_once()
    database.close.assert_called_once()


@pytest.mark.asyncio
async def test_unexpected_native_tools_fail_closed(native):
    runtime, agent, _, _ = native
    agent.tools = [{"function": {"name": "example"}}]
    with pytest.raises(RuntimeError, match="unexpectedly enabled tools"):
        await runtime.send_command(
            OperatorCommand(command_id="example", text="hello", session_mode="new")
        )
    agent.run_conversation.assert_not_called()
