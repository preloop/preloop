"""Guards on the console-nginx and ingress proxy settings for gateway routes.

The model-gateway routes (``/openai``, ``/anthropic``, ``/gemini``) proxy
streaming LLM responses. A large prompt can keep the upstream model thinking
for well over a minute before the first token appears, and nginx's default
``proxy_read_timeout`` is 60s — so a route without an explicit override kills
the connection and hands the client a 504 that never reaches the application.

Both proxy layers in front of the gateway (the console nginx and the ingress)
default to 60s, so both need the override; fixing one still leaves the other
at 60s. These tests exist so a future edit that drops either one fails CI
instead of shipping.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "helm" / "preloop"
NGINX_CONFIGMAP = CHART_DIR / "templates" / "configmap-nginx.yaml"
CHART_VALUES = CHART_DIR / "values.yaml"
DOCKER_NGINX_TEMPLATE = REPO_ROOT / "frontend" / "docker" / "nginx.conf.template"

# The gateway route prefixes that carry streaming model traffic.
GATEWAY_LOCATIONS = ("^~ /openai/", "^~ /anthropic/", "^~ /gemini/")

# A streaming request must survive a slow first token. Anything at or below
# nginx's 60s default reintroduces the bug this guard exists for.
MIN_STREAMING_TIMEOUT_SECONDS = 300


def _load_values() -> Dict:
    """Return the chart's default values."""
    return yaml.safe_load(CHART_VALUES.read_text())


def _resolve_values_path(values: Dict, dotted: str):
    """Look up a dotted ``.Values`` path, returning None when absent."""
    node = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _render_with_default_values(template_text: str) -> str:
    """Substitute ``.Values`` references so the config can be parsed.

    This is a deliberately tiny stand-in for Helm: it resolves
    ``{{ .Values.a.b }}`` from values.yaml and drops every other action. It
    lets the assertions run in a test environment that has no ``helm``
    binary; when helm *is* available a separate test checks the real render.
    """
    values = _load_values()

    def _replace(match: re.Match) -> str:
        expression = match.group(1).strip()
        values_ref = re.fullmatch(r"\.Values\.([\w.]+)", expression)
        if values_ref:
            resolved = _resolve_values_path(values, values_ref.group(1))
            if resolved is not None:
                return str(resolved)
        return ""

    return re.sub(r"{{-?\s*(.*?)\s*-?}}", _replace, template_text, flags=re.DOTALL)


def _nginx_config_from_configmap(rendered_yaml: str) -> str:
    """Pull the nginx server config out of the rendered ConfigMap."""
    document = yaml.safe_load(rendered_yaml)
    return document["data"]["default.conf.template"]


def _location_body(config: str, location: str) -> str:
    """Return the directives inside one ``location`` block.

    Args:
        config: The nginx configuration text.
        location: The location matcher exactly as written in the config.

    Returns:
        Everything between the block's braces.
    """
    marker = f"location {location} {{"
    start = config.find(marker)
    assert start != -1, f"no `location {location}` block found in the nginx config"

    depth = 0
    cursor = start + len(marker) - 1
    for index in range(cursor, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[cursor + 1 : index]
    raise AssertionError(f"unterminated `location {location}` block")


def _directive_seconds(body: str, directive: str) -> int:
    """Return a timeout directive's value in seconds."""
    match = re.search(rf"^\s*{directive}\s+(\d+)(m?s)\s*;", body, re.MULTILINE)
    assert match, f"`{directive}` is not set on this location"
    seconds = int(match.group(1))
    return seconds // 1000 if match.group(2) == "ms" else seconds


def _gateway_nginx_configs() -> List[pytest.param]:
    """Every nginx config in the repo that fronts the model gateway."""
    return [
        pytest.param(NGINX_CONFIGMAP, True, id="helm-configmap"),
        pytest.param(DOCKER_NGINX_TEMPLATE, False, id="docker-compose"),
    ]


def _read_nginx_config(path: Path, is_configmap: bool) -> str:
    text = _render_with_default_values(path.read_text())
    return _nginx_config_from_configmap(text) if is_configmap else text


@pytest.mark.parametrize("config_path,is_configmap", _gateway_nginx_configs())
@pytest.mark.parametrize("location", GATEWAY_LOCATIONS)
def test_gateway_location_survives_a_slow_first_token(
    config_path: Path, is_configmap: bool, location: str
) -> None:
    """Streaming gateway routes must not inherit nginx's 60s read timeout."""
    body = _location_body(_read_nginx_config(config_path, is_configmap), location)

    assert _directive_seconds(body, "proxy_read_timeout") >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    ), f"`location {location}` would 504 a slow-to-start stream"
    assert (
        _directive_seconds(body, "proxy_send_timeout") >= MIN_STREAMING_TIMEOUT_SECONDS
    )
    assert _directive_seconds(body, "proxy_connect_timeout") > 0


@pytest.mark.parametrize("config_path,is_configmap", _gateway_nginx_configs())
@pytest.mark.parametrize("location", GATEWAY_LOCATIONS)
def test_gateway_location_streams_tokens_through(
    config_path: Path, is_configmap: bool, location: str
) -> None:
    """Buffering an SSE stream delays the first byte the client sees."""
    body = _location_body(_read_nginx_config(config_path, is_configmap), location)

    assert re.search(r"^\s*proxy_buffering\s+off\s*;", body, re.MULTILINE), (
        f"`location {location}` buffers the SSE stream instead of relaying it"
    )


def test_ingress_annotations_match_the_console_timeout() -> None:
    """The ingress is the second 60s proxy in front of the gateway."""
    values = _load_values()
    read_timeout = _resolve_values_path(values, "gateway.proxy.readTimeout")
    send_timeout = _resolve_values_path(values, "gateway.proxy.sendTimeout")

    assert read_timeout is not None and int(read_timeout) >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )
    assert send_timeout is not None and int(send_timeout) >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )

    rendered = _render_ingress()
    annotations = rendered["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == str(
        read_timeout
    )
    assert annotations["nginx.ingress.kubernetes.io/proxy-send-timeout"] == str(
        send_timeout
    )


def test_ingress_annotations_stay_overridable() -> None:
    """An operator's own annotation value must win over the chart default."""
    rendered = _render_ingress(
        overrides=[
            "ingress.annotations.nginx\\.ingress\\.kubernetes\\.io/proxy-read-timeout=1200"
        ]
    )
    annotations = rendered["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "1200"
    # The cert-manager default is still there: overriding one key must not
    # replace the whole annotation map.
    assert "cert-manager.io/cluster-issuer" in annotations


@contextmanager
def _offline_chart() -> Iterator[Path]:
    """Yield a copy of the chart that renders without fetching dependencies.

    ``helm template`` refuses to run while a declared subchart is missing from
    ``charts/``, which would make these tests require network access. The
    subcharts are irrelevant to the templates under test, so the copy simply
    declares none.
    """
    with tempfile.TemporaryDirectory() as tmp:
        chart_copy = Path(tmp) / CHART_DIR.name
        shutil.copytree(CHART_DIR, chart_copy)
        chart_yaml = chart_copy / "Chart.yaml"
        metadata = yaml.safe_load(chart_yaml.read_text())
        metadata.pop("dependencies", None)
        chart_yaml.write_text(yaml.safe_dump(metadata))
        yield chart_copy


def _helm_template(template: str, overrides: List[str] | None = None) -> str:
    """Render one chart template, skipping when helm is unavailable."""
    helm = shutil.which("helm")
    if helm is None:  # pragma: no cover - depends on the local toolchain
        pytest.skip("helm binary not available")

    with _offline_chart() as chart_dir:
        command = [helm, "template", "preloop", str(chart_dir), "--show-only", template]
        for override in overrides or []:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    return result.stdout


def _render_ingress(overrides: List[str] | None = None) -> Dict:
    """Render the ingress template with the chart's default values."""
    rendered = _helm_template(
        "templates/ingress.yaml",
        ["ingress.enabled=true", *(overrides or [])],
    )
    return yaml.safe_load(rendered)


@pytest.mark.parametrize("location", GATEWAY_LOCATIONS)
def test_helm_render_keeps_the_gateway_timeouts(location: str) -> None:
    """The same guard, against a real ``helm template`` render."""
    rendered = _helm_template("templates/configmap-nginx.yaml")

    body = _location_body(_nginx_config_from_configmap(rendered), location)
    assert _directive_seconds(body, "proxy_read_timeout") >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )
    assert re.search(r"^\s*proxy_buffering\s+off\s*;", body, re.MULTILINE)
