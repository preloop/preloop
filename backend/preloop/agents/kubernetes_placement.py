"""Runtime and node placement for Kubernetes agent pods.

The Helm chart passes these settings to every process that can launch an
agent Job through the deployment environment:

* ``AGENT_RUNTIME_CLASS_NAME`` -> ``spec.runtimeClassName``
* ``AGENT_NODE_SELECTOR`` -> ``spec.nodeSelector`` (JSON object)
* ``AGENT_TOLERATIONS`` -> ``spec.tolerations`` (JSON list)

RuntimeClass is how an operator pins agent pods to a stronger sandbox
runtime (Kata Containers, gVisor, Firecracker). Node selection and
tolerations keep the agent pods off the control-plane nodes or on the
tainted pool that provides that runtime.

Unset or malformed values are ignored so a typo in one setting never
prevents an agent from starting: the pod falls back to the cluster
defaults, exactly as it did before this setting existed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

RUNTIME_CLASS_NAME_ENV = "AGENT_RUNTIME_CLASS_NAME"
NODE_SELECTOR_ENV = "AGENT_NODE_SELECTOR"
TOLERATIONS_ENV = "AGENT_TOLERATIONS"


def _json_env(name: str, default: Any) -> Any:
    """Decode a JSON environment variable, ignoring anything unusable.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset, invalid JSON,
            or decodes to the wrong type.

    Returns:
        The decoded value or ``default``.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring %s: value is not valid JSON", name)
        return default
    if not isinstance(value, type(default)):
        logger.warning("Ignoring %s: expected a %s", name, type(default).__name__)
        return default
    return value


def runtime_class_name() -> str:
    """Return the RuntimeClass configured for agent pods.

    Returns:
        The configured class name, or an empty string when unset.
    """
    return os.getenv(RUNTIME_CLASS_NAME_ENV, "").strip()


def node_selector() -> Dict[str, str]:
    """Return the node selector configured for agent pods.

    Returns:
        A mapping of label key to value. Empty when unset or malformed.
        Values are stringified because Kubernetes requires string labels.
    """
    raw = _json_env(NODE_SELECTOR_ENV, {})
    return {str(key): str(value) for key, value in raw.items()}


def tolerations() -> List[Dict[str, Any]]:
    """Return the tolerations configured for agent pods.

    Returns:
        A list of toleration mappings. Empty when unset. Entries that are
        not objects are dropped rather than failing the whole Job.
    """
    raw = _json_env(TOLERATIONS_ENV, [])
    return [entry for entry in raw if isinstance(entry, dict)]
