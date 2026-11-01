"""Column types that keep stored credentials encrypted at rest.

Values are sealed with :func:`preloop.utils.encryption.encrypt_value`, the
Fernet layer that already protects ``secret_reference.encrypted_value``,
OAuth tokens and webhook endpoint secrets. No new key is introduced: the key
comes from ``SECURITY__ENCRYPTION_KEY`` or is derived from
``SECURITY__SECRET_KEY`` exactly as for those columns.

A sealed value is stored as ``enc:v1:<fernet token>``. The prefix makes
sealing idempotent and lets readers accept rows written before this change:
an unprefixed value is returned as is until the credential backfill
(:mod:`preloop.models.credential_encryption`) seals it in place. ORM readers
always see plaintext, so callers do not change.

Two types are provided:

* :class:`EncryptedText` for a column that holds one credential.
* :class:`SecretFieldsJSON` for a JSON document that holds credentials in
  some of its fields. A named selector decides which string leaves are
  sealed; the rest of the document stays queryable and readable.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

from sqlalchemy import JSON, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

#: Marks a value sealed by this module.
SEALED_PREFIX = "enc:v1:"

#: Key that holds an inbound webhook signing secret in flow and approval
#: workflow configuration documents.
WEBHOOK_SECRET_KEY = "webhook_secret"

#: Credential keys flow and approval workflow configuration documents carry:
#: webhook and employee-event signing secrets and the AI approval provider key.
CONFIG_SECRET_KEYS = frozenset({WEBHOOK_SECRET_KEY, "employee_secret", "api_key"})

Transform = Callable[[str], str]


def is_sealed(value: Any) -> bool:
    """Return True when ``value`` is a string sealed by :func:`seal`."""
    return isinstance(value, str) and value.startswith(SEALED_PREFIX)


def seal(value: str) -> str:
    """Encrypt one credential for storage.

    Empty strings and values that are already sealed are returned unchanged,
    so sealing twice is a no-op.

    Args:
        value: Plaintext credential.

    Returns:
        ``enc:v1:`` followed by a Fernet token, or the input when empty or
        already sealed.

    Raises:
        ValueError: If the round trip with the configured key fails.
    """
    if not value or is_sealed(value):
        return value
    from preloop.utils.encryption import decrypt_value, encrypt_value

    token = encrypt_value(value)
    # Never store a value the configured key cannot read back.
    if decrypt_value(token) != value:
        raise ValueError("Credential encryption round trip failed")
    return SEALED_PREFIX + token


def unseal(value: str) -> str:
    """Return the plaintext for a stored value.

    Unprefixed values were written before encryption at rest and are returned
    unchanged.

    Args:
        value: Stored value.

    Returns:
        The plaintext credential.

    Raises:
        ValueError: If a sealed value cannot be decrypted with the configured
            key.
    """
    if not is_sealed(value):
        return value
    from preloop.utils.encryption import decrypt_value

    return decrypt_value(value[len(SEALED_PREFIX) :])


def _is_mcp_secret_key(key: Any) -> bool:
    # Local import: schemas import models at load time.
    from preloop.models.schemas.mcp_server import _is_secret_auth_key

    return bool(_is_secret_auth_key(key))


def _map_secret_leaves(
    value: Any, transform: Transform, is_secret: Callable[[Any], bool]
) -> Any:
    """Apply ``transform`` to every string leaf stored under a secret key."""
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and is_secret(key):
                result[key] = transform(item)
            else:
                result[key] = _map_secret_leaves(item, transform, is_secret)
        return result
    if isinstance(value, list):
        return [_map_secret_leaves(item, transform, is_secret) for item in value]
    return value


def _mcp_auth_config(value: Any, transform: Transform) -> Any:
    return _map_secret_leaves(value, transform, _is_mcp_secret_key)


def _is_config_secret_key(key: Any) -> bool:
    return key in CONFIG_SECRET_KEYS


def _webhook_secret(value: Any, transform: Transform) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        key: transform(item)
        if isinstance(item, str) and _is_config_secret_key(key)
        else item
        for key, item in value.items()
    }


def _is_agent_config_secret_key(key: Any) -> bool:
    from preloop.utils.redaction import _is_sensitive_key

    return bool(_is_sensitive_key(key))


def _agent_config(value: Any, transform: Transform) -> Any:
    """Seal credentials in captured or managed agent configuration."""
    return _map_secret_leaves(value, transform, _is_agent_config_secret_key)


def _policy_snapshot(value: Any, transform: Transform) -> Any:
    """Seal MCP server credentials and webhook secrets kept for rollback."""
    if not isinstance(value, dict):
        return value
    result = {
        key: item
        if key == "mcp_servers"
        else _map_secret_leaves(item, transform, _is_config_secret_key)
        for key, item in value.items()
    }
    servers = result.get("mcp_servers")
    if isinstance(servers, list):
        result["mcp_servers"] = [
            (
                {
                    **server,
                    "auth_config": _mcp_auth_config(server["auth_config"], transform),
                }
                if isinstance(server, dict) and server.get("auth_config")
                else server
            )
            for server in servers
        ]
    return result


#: Selector name -> function that applies a transform to the secret fields of
#: a document. Names are stored in the column type, so they must stay stable.
SECRET_SELECTORS: dict[str, Callable[[Any, Transform], Any]] = {
    "mcp_auth_config": _mcp_auth_config,
    "webhook_secret": _webhook_secret,
    "policy_snapshot": _policy_snapshot,
    "agent_config": _agent_config,
}


def seal_document(selector: str, value: Any) -> Any:
    """Return a copy of ``value`` with its secret fields sealed."""
    if value is None:
        return None
    return SECRET_SELECTORS[selector](copy.deepcopy(value), seal)


def unseal_document(selector: str, value: Any) -> Any:
    """Return a copy of ``value`` with its secret fields decrypted."""
    if value is None:
        return None
    return SECRET_SELECTORS[selector](copy.deepcopy(value), unseal)


def count_plaintext_fields(selector: str, value: Any) -> int:
    """Count secret fields of ``value`` that are stored unsealed."""
    found = 0

    def _count(item: str) -> str:
        nonlocal found
        if item and not is_sealed(item):
            found += 1
        return item

    if value is not None:
        SECRET_SELECTORS[selector](value, _count)
    return found


class EncryptedText(TypeDecorator[str]):
    """Text column whose value is sealed on write and decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect: Dialect) -> Any:
        """Seal a plaintext value before it reaches the database."""
        if value is None:
            return None
        return seal(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Optional[str]:
        """Decrypt a sealed value; return legacy plaintext unchanged."""
        if value is None:
            return None
        return unseal(value)


class SecretFieldsJSON(TypeDecorator[Any]):
    """JSON column whose secret fields are sealed on write.

    Args:
        selector: Name in :data:`SECRET_SELECTORS` choosing the secret fields.
        jsonb: Store as PostgreSQL JSONB instead of JSON.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, selector: str, jsonb: bool = False) -> None:
        if selector not in SECRET_SELECTORS:
            raise ValueError(f"Unknown secret selector: {selector}")
        super().__init__()
        self.selector = selector
        self.jsonb = jsonb
        if jsonb:
            # Keep JSONB operators (``astext``, ``has_key``) on the column.
            self.impl = JSONB()

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Use JSONB on PostgreSQL when requested."""
        if self.jsonb and dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """Seal secret fields before the document is serialized."""
        return seal_document(self.selector, value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        """Decrypt secret fields of a loaded document."""
        return unseal_document(self.selector, value)
