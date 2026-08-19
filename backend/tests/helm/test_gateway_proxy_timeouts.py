"""Guards on the console-nginx and ingress proxy settings for gateway routes.

The model-gateway routes (``/openai``, ``/anthropic``, ``/gemini``) proxy
streaming LLM responses. A large prompt can keep the upstream model thinking
for well over a minute before the first token appears, and nginx's default
``proxy_read_timeout`` is 60s — so a route without an explicit override kills
the connection and hands the client a 504 that never reaches the application.

Helm ingress sends those prefixes to the gateway Service. Console nginx still
proxies them for callers that hit the console Service directly. Both of those
proxies default to 60s, so both need the override; fixing one still leaves
the other at 60s for the traffic that still uses it. These tests exist so a
future edit that drops either one fails CI instead of shipping.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from tests.helm.chart_helpers import (
    CHART_DIR,
    REPO_ROOT,
    helm_template,
    load_values,
    resolve_values_path,
)

NGINX_CONFIGMAP = CHART_DIR / "templates" / "configmap-nginx.yaml"
DOCKER_NGINX_TEMPLATE = REPO_ROOT / "frontend" / "docker" / "nginx.conf.template"

# The gateway route prefixes that carry streaming model traffic.
GATEWAY_LOCATIONS = ("^~ /openai/", "^~ /anthropic/", "^~ /gemini/")

# A streaming request must survive a slow first token. Anything at or below
# nginx's 60s default reintroduces the bug this guard exists for.
MIN_STREAMING_TIMEOUT_SECONDS = 300


def _render_with_default_values(template_text: str) -> str:
    """Substitute ``.Values`` references so the config can be parsed.

    This is a deliberately tiny stand-in for Helm: it resolves
    ``{{ .Values.a.b }}`` from values.yaml and drops every other action. It
    lets the assertions run in a test environment that has no ``helm``
    binary; when helm *is* available a separate test checks the real render.
    """
    values = load_values()

    def _replace(match: re.Match) -> str:
        expression = match.group(1).strip()
        values_ref = re.fullmatch(r"\.Values\.([\w.]+)", expression)
        if values_ref:
            resolved = resolve_values_path(values, values_ref.group(1))
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


@pytest.mark.parametrize("location", GATEWAY_LOCATIONS)
def test_install_oss_tls_proxy_sends_gateway_routes_direct(location: str) -> None:
    """The OSS TLS edge proxy must not hairpin /openai through the console.

    A 2026-08-18 co-located bench showed ~70ms extra TTFB when public nginx
    sent every path to console nginx, which then proxied to the gateway.
    Helm ingress and this installer overlay both skip that hop; console
    nginx still proxies the prefixes for in-cluster callers.
    """
    text = (REPO_ROOT / "scripts" / "install-oss.sh").read_text()
    marker = f"location {location} {{"
    assert marker in text, f"install-oss.sh missing `{marker}`"
    body = _location_body(text, location)
    assert "proxy_pass http://gateway:8000;" in body
    assert re.search(r"^\s*proxy_buffering\s+off\s*;", body, re.MULTILINE)
    assert _directive_seconds(body, "proxy_read_timeout") >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )


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
    """The console Ingress still carries the streaming timeout budget."""
    values = load_values()
    read_timeout = resolve_values_path(values, "gateway.proxy.readTimeout")
    send_timeout = resolve_values_path(values, "gateway.proxy.sendTimeout")

    assert read_timeout is not None and int(read_timeout) >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )
    assert send_timeout is not None and int(send_timeout) >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )

    rendered = _render_console_ingress()
    annotations = rendered["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == str(
        read_timeout
    )
    assert annotations["nginx.ingress.kubernetes.io/proxy-send-timeout"] == str(
        send_timeout
    )


def test_ingress_annotations_stay_overridable() -> None:
    """An operator's own annotation value must win over the chart default."""
    rendered = _render_console_ingress(
        overrides=[
            "ingress.annotations.nginx\\.ingress\\.kubernetes\\.io/proxy-read-timeout=1200"
        ]
    )
    annotations = rendered["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "1200"
    # The cert-manager default is still there: overriding one key must not
    # replace the whole annotation map.
    assert "cert-manager.io/cluster-issuer" in annotations


def test_ingress_sends_gateway_prefixes_to_gateway_service() -> None:
    """Public /openai traffic must not hairpin through the console Service."""
    gateway = _render_gateway_ingress()
    assert gateway["metadata"]["name"] == "preloop-gateway"
    paths = {
        item["path"]: item["backend"]["service"]["name"]
        for item in gateway["spec"]["rules"][0]["http"]["paths"]
    }
    assert paths == {
        "/openai": "preloop-gateway",
        "/anthropic": "preloop-gateway",
        "/gemini": "preloop-gateway",
    }

    console_paths = {
        item["path"]
        for item in _render_console_ingress()["spec"]["rules"][0]["http"]["paths"]
    }
    assert "/openai" not in console_paths
    assert "/anthropic" not in console_paths
    assert "/gemini" not in console_paths


def test_gateway_ingress_disables_buffering_and_keeps_timeouts() -> None:
    """SSE must not buffer at ingress once console nginx is no longer in path."""
    values = load_values()
    read_timeout = str(resolve_values_path(values, "gateway.proxy.readTimeout"))
    gateway = _render_gateway_ingress()
    annotations = gateway["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == read_timeout
    assert "cert-manager.io/cluster-issuer" not in annotations
    console = _render_console_ingress()
    assert (
        "nginx.ingress.kubernetes.io/proxy-buffering"
        not in console["metadata"]["annotations"]
    )
    assert "cert-manager.io/cluster-issuer" in console["metadata"]["annotations"]


def _render_ingress_docs(overrides: List[str] | None = None) -> List[Dict]:
    """Render every Ingress document in the chart template."""
    rendered = helm_template(
        "templates/ingress.yaml",
        ["ingress.enabled=true", *(overrides or [])],
    )
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _render_console_ingress(overrides: List[str] | None = None) -> Dict:
    """Render the console Ingress (first document, name without -gateway)."""
    docs = _render_ingress_docs(overrides)
    console = next(
        doc
        for doc in docs
        if doc["kind"] == "Ingress" and not doc["metadata"]["name"].endswith("-gateway")
    )
    return console


def _render_gateway_ingress(overrides: List[str] | None = None) -> Dict:
    """Render the gateway Ingress that owns /openai /anthropic /gemini."""
    docs = _render_ingress_docs(overrides)
    gateway = next(
        doc
        for doc in docs
        if doc["kind"] == "Ingress" and doc["metadata"]["name"].endswith("-gateway")
    )
    return gateway


@pytest.mark.parametrize("location", GATEWAY_LOCATIONS)
def test_helm_render_keeps_the_gateway_timeouts(location: str) -> None:
    """The same guard, against a real ``helm template`` render."""
    rendered = helm_template("templates/configmap-nginx.yaml")

    body = _location_body(_nginx_config_from_configmap(rendered), location)
    assert _directive_seconds(body, "proxy_read_timeout") >= (
        MIN_STREAMING_TIMEOUT_SECONDS
    )
    assert re.search(r"^\s*proxy_buffering\s+off\s*;", body, re.MULTILINE)
