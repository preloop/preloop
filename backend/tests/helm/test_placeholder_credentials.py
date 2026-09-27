"""Guards for the fail-closed JWT signing key in the Helm chart.

The chart used to ship a published placeholder as ``environment.jwtSecret``,
so every install that kept the default signed access tokens with a key
anyone could read on GitHub. ``templates/secret.yaml`` now refuses to render
while the value is empty or a known placeholder, unless ``existingSecret``
supplies the key. These tests pin that refusal and its escape hatches.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from typing import List

import pytest

from tests.helm.chart_helpers import (
    helm_template,
    helm_template_all,
    load_values,
    offline_chart,
)

REAL_SECRET = "0f3a1b2c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8"


def _render_expecting_failure(overrides: List[str]) -> str:
    """Render the whole chart and assert helm refuses, returning stderr.

    Args:
        overrides: ``--set`` overrides passed to ``helm template``.

    Returns:
        The helm error output, for message assertions.
    """
    helm = shutil.which("helm")
    if helm is None:  # pragma: no cover - depends on the local toolchain
        pytest.skip("helm binary not available")

    with offline_chart() as chart_dir:
        command = [helm, "template", "preloop", str(chart_dir)]
        for override in overrides:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0, (
        "helm template succeeded although the jwtSecret guard should refuse: "
        f"{overrides}"
    )
    return result.stderr


def test_default_jwt_secret_is_empty() -> None:
    """The chart ships no usable signing key; the guard makes it required."""
    assert load_values()["environment"]["jwtSecret"] == ""


def test_empty_jwt_secret_refuses_to_install() -> None:
    """Default values must not render: an empty signing key fails closed."""
    stderr = _render_expecting_failure([])
    assert "jwtSecret" in stderr
    assert "openssl rand -hex 32" in stderr


@pytest.mark.parametrize(
    "placeholder",
    [
        "change-this-in-production",
        "CHANGE-THIS-IN-PRODUCTION",
        "change_this_in_production",
        "changeme",
        "development_secret_key_do_not_use_in_production",
    ],
)
def test_placeholder_jwt_secret_refuses_to_install(placeholder: str) -> None:
    """Published placeholder signing keys are rejected regardless of case."""
    stderr = _render_expecting_failure([f"environment.jwtSecret={placeholder}"])
    assert "placeholder" in stderr


def test_real_jwt_secret_renders_the_chart_secret() -> None:
    """A real signing key renders and lands base64-encoded in the Secret."""
    rendered = helm_template(
        "templates/secret.yaml",
        overrides=[f"environment.jwtSecret={REAL_SECRET}"],
    )
    encoded = base64.b64encode(REAL_SECRET.encode()).decode()
    assert f'jwt-secret: "{encoded}"' in rendered


def test_existing_secret_bypasses_the_guard() -> None:
    """``existingSecret`` supplies the key, so no chart value is required."""
    rendered = helm_template_all(
        overrides=["existingSecret=preloop-app", "environment.jwtSecret="]
    )
    assert "jwt-secret:" not in rendered
