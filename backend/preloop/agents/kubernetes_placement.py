"""Runtime and node placement for Kubernetes agent pods.

The Helm chart passes these settings to every process that can launch an
agent Job through the deployment environment:

* ``AGENT_RUNTIME_CLASS_NAME`` -> ``spec.runtimeClassName``
* ``AGENT_NODE_SELECTOR`` -> ``spec.nodeSelector`` (JSON object)
* ``AGENT_TOLERATIONS`` -> ``spec.tolerations`` (JSON list)

RuntimeClass is how an operator pins agent pods to a stronger sandbox
runtime (Kata Containers, gVisor, Firecracker). Node selection and
tolerations keep the agent pods off the control-plane nodes or on the
tainted pool that provides that runtime. The same helpers feed the hosted
publication verifier Job, which also runs untrusted repository code.

Unset or malformed values are ignored so a typo in one setting never
prevents an agent from starting: the pod falls back to the cluster
defaults, exactly as it did before this setting existed. Values that parse
as JSON but violate a Kubernetes constraint (an unknown toleration effect,
a non-integer ``tolerationSeconds`` or one without ``NoExecute``, an
invalid label key or value) are likewise
dropped or normalized, because the API server would otherwise reject the
whole Job and no agent would start.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RUNTIME_CLASS_NAME_ENV = "AGENT_RUNTIME_CLASS_NAME"
NODE_SELECTOR_ENV = "AGENT_NODE_SELECTOR"
TOLERATIONS_ENV = "AGENT_TOLERATIONS"

# Kubernetes label grammar, see
# https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?$")

_TOLERATION_EFFECTS = frozenset({"NoSchedule", "PreferNoSchedule", "NoExecute"})
_TOLERATION_OPERATORS = frozenset({"Equal", "Exists"})


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


def _is_label_key(key: Any) -> bool:
    """Return whether ``key`` is a syntactically valid Kubernetes label key."""
    if not isinstance(key, str) or not key:
        return False
    prefix, separator, name = key.rpartition("/")
    if not separator:
        prefix, name = "", key
    if not name or len(name) > 63 or not _LABEL_NAME.match(name):
        return False
    if prefix and (len(prefix) > 253 or not _LABEL_PREFIX.match(prefix)):
        return False
    return True


def _is_label_value(value: Any) -> bool:
    """Return whether ``value`` is a syntactically valid Kubernetes label value."""
    if not isinstance(value, str):
        return False
    return len(value) <= 63 and bool(_LABEL_VALUE.match(value))


def _coerce_toleration_seconds(value: Any) -> Optional[int]:
    """Return a non-negative integer for ``tolerationSeconds``, or ``None``."""
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass, but Kubernetes rejects it.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _normalize_toleration(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a Kubernetes-safe toleration, or ``None`` when unusable.

    Fields the API server enum/type-validates are checked so a single typo
    cannot make ``create_namespaced_job`` fail.
    """
    key = entry.get("key")
    if key is not None and not _is_label_key(key):
        logger.warning("Ignoring toleration with an invalid key")
        return None

    operator = entry.get("operator")
    # Check the type first: a JSON list or object is unhashable and would
    # raise TypeError from the frozenset membership test.
    if operator is not None and (
        not isinstance(operator, str) or operator not in _TOLERATION_OPERATORS
    ):
        logger.warning("Ignoring toleration with an invalid operator")
        return None

    effect = entry.get("effect")
    if effect is not None and effect != "" and (
        not isinstance(effect, str) or effect not in _TOLERATION_EFFECTS
    ):
        logger.warning("Ignoring toleration with an invalid effect")
        return None

    value = entry.get("value")
    if value is not None and not isinstance(value, str):
        logger.warning("Ignoring toleration with a non-string value")
        return None
    if operator == "Exists":
        # The API server rejects a value on an ``Exists`` toleration.
        value = None
    elif value is not None and not _is_label_value(value):
        # ``Equal`` (or no operator) values must be valid label values.
        logger.warning("Ignoring toleration with an invalid value")
        return None

    toleration_seconds = entry.get("tolerationSeconds")
    if toleration_seconds is not None:
        toleration_seconds = _coerce_toleration_seconds(toleration_seconds)
        if toleration_seconds is None:
            logger.warning("Ignoring toleration with an invalid tolerationSeconds")
            return None
        if effect != "NoExecute":
            # The API server only accepts tolerationSeconds with NoExecute.
            logger.warning("Ignoring toleration with tolerationSeconds but no NoExecute")
            return None

    normalized: Dict[str, Any] = {}
    if key is not None:
        normalized["key"] = key
    if operator is not None:
        normalized["operator"] = operator
    if value:
        normalized["value"] = value
    if effect:
        normalized["effect"] = effect
    if toleration_seconds is not None:
        normalized["tolerationSeconds"] = toleration_seconds
    return normalized


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
        Pairs whose key or value violates label syntax are dropped, since
        the API server would reject the whole Job otherwise.
    """
    raw = _json_env(NODE_SELECTOR_ENV, {})
    selector: Dict[str, str] = {}
    for key, value in raw.items():
        if not _is_label_key(key):
            logger.warning("Ignoring node selector with an invalid key")
            continue
        text = str(value)
        if not _is_label_value(text):
            logger.warning("Ignoring node selector with an invalid value")
            continue
        selector[key] = text
    return selector


def tolerations() -> List[Dict[str, Any]]:
    """Return the tolerations configured for agent pods.

    Returns:
        A list of toleration mappings. Empty when unset. Entries that are
        not objects, or whose enum/typed fields are invalid, are dropped
        rather than failing the whole Job.
    """
    raw = _json_env(TOLERATIONS_ENV, [])
    normalized: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("Ignoring toleration entry that is not an object")
            continue
        toleration = _normalize_toleration(entry)
        if toleration is not None:
            normalized.append(toleration)
    return normalized


def client_tolerations(client: Any) -> List[Any]:
    """Build ``V1Toleration`` objects from the configured tolerations.

    Args:
        client: The ``kubernetes_asyncio.client`` module. It is injected so
            this module stays importable without the Kubernetes SDK.

    Returns:
        Toleration objects ready for ``V1PodSpec.tolerations``. Empty when
        nothing valid is configured.
    """
    return [
        client.V1Toleration(
            key=item.get("key"),
            operator=item.get("operator"),
            value=item.get("value"),
            effect=item.get("effect"),
            toleration_seconds=item.get("tolerationSeconds"),
        )
        for item in tolerations()
    ]
