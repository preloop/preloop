"""Tests for shared Kubernetes environment detection."""

import os
from unittest.mock import patch

from preloop.agents.kubernetes import (
    K8S_SERVICE_ACCOUNT_TOKEN,
    detect_kubernetes_environment,
)


class TestDetectKubernetesEnvironment:
    def test_explicit_true(self):
        with patch.dict(os.environ, {"USE_KUBERNETES": "true"}):
            assert detect_kubernetes_environment() is True

    def test_explicit_false(self):
        with patch.dict(os.environ, {"USE_KUBERNETES": "false"}):
            assert detect_kubernetes_environment() is False

    def test_service_host_detection(self):
        with patch.dict(
            os.environ,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "USE_KUBERNETES": ""},
        ):
            with patch("os.path.exists", return_value=False):
                assert detect_kubernetes_environment() is True

    def test_service_account_token(self):
        with patch.dict(
            os.environ, {"USE_KUBERNETES": "", "KUBERNETES_SERVICE_HOST": ""}
        ):
            with patch(
                "os.path.exists",
                side_effect=lambda p: p == K8S_SERVICE_ACCOUNT_TOKEN,
            ):
                assert detect_kubernetes_environment() is True

    def test_defaults_to_docker(self):
        with patch.dict(
            os.environ, {"USE_KUBERNETES": "", "KUBERNETES_SERVICE_HOST": ""}
        ):
            with patch("os.path.exists", return_value=False):
                assert detect_kubernetes_environment() is False
