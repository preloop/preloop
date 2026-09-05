"""Detect whether hosted agents should run as Kubernetes Jobs."""

import logging
import os

logger = logging.getLogger(__name__)

K8S_SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def detect_kubernetes_environment() -> bool:
    """Return True when hosted agents should use Kubernetes instead of Docker.

    Precedence: ``USE_KUBERNETES`` (true/false) overrides auto-detection.
    Otherwise a service-account token or ``KUBERNETES_SERVICE_HOST`` means
    we are in-cluster. Default is Docker.
    """
    env_value = os.getenv("USE_KUBERNETES", "").lower()
    if env_value == "true":
        logger.info("Kubernetes mode enabled via USE_KUBERNETES=true")
        return True
    if env_value == "false":
        logger.info("Kubernetes mode disabled via USE_KUBERNETES=false")
        return False

    if os.path.exists(K8S_SERVICE_ACCOUNT_TOKEN):
        logger.info(
            "Kubernetes environment detected (found service account token at %s)",
            K8S_SERVICE_ACCOUNT_TOKEN,
        )
        return True

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        logger.info("Kubernetes environment detected (KUBERNETES_SERVICE_HOST present)")
        return True

    logger.info("No Kubernetes environment detected, defaulting to Docker mode")
    return False
