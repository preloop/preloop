"""Detect managed execution or agent API keys attached during auth."""

from typing import Any


def is_managed_execution_credential(current_user: Any) -> bool:
    """True when this principal authenticated with a managed execution key.

    Reads the API key attached by JWT/API-key auth, not agent-supplied
    body fields. JWT console sessions have no ``_auth_api_key``. Personal
    keys without ``flow_execution_id`` or ``managed_agent_id`` are not
    managed execution credentials. Non-dict ``context_data`` is ignored
    so mocked users without a real key stay allowed.
    """
    api_key = getattr(current_user, "_auth_api_key", None)
    if api_key is None:
        return False
    context = api_key.context_data if isinstance(api_key.context_data, dict) else {}
    return bool(context.get("flow_execution_id") or context.get("managed_agent_id"))
