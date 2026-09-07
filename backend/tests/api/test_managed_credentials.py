"""Managed execution credentials are detected from the auth-time API key."""

from types import SimpleNamespace

from preloop.api.managed_credentials import is_managed_execution_credential


def test_is_managed_execution_credential_reads_auth_api_key_context() -> None:
    assert is_managed_execution_credential(SimpleNamespace()) is False
    assert is_managed_execution_credential(SimpleNamespace(_auth_api_key=None)) is False
    personal = SimpleNamespace(_auth_api_key=SimpleNamespace(context_data={}))
    assert is_managed_execution_credential(personal) is False
    ignored = SimpleNamespace(_auth_api_key=SimpleNamespace(context_data="not-a-dict"))
    assert is_managed_execution_credential(ignored) is False
    by_execution = SimpleNamespace(
        _auth_api_key=SimpleNamespace(context_data={"flow_execution_id": "exec-1"})
    )
    assert is_managed_execution_credential(by_execution) is True
    by_agent = SimpleNamespace(
        _auth_api_key=SimpleNamespace(context_data={"managed_agent_id": "agent-1"})
    )
    assert is_managed_execution_credential(by_agent) is True
