"""Rendered-Helm guards for console security headers and HSTS (#2278, #2279)."""

from __future__ import annotations

import re
import subprocess
from typing import Dict

import pytest
import yaml

from tests.helm.chart_helpers import (
    TEST_JWT_SECRET,
    helm_template,
    load_values,
    offline_chart,
    resolve_values_path,
)
from tests.test_nginx_security_headers import (
    DOCKER_SNIPPET,
    blocks_with_own_add_header,
    parse_header_lines,
)

CONFIGMAP = "templates/configmap-nginx.yaml"


def _rendered_nginx(overrides: list[str] | None = None) -> str:
    rendered = helm_template(CONFIGMAP, overrides)
    return yaml.safe_load(rendered)["data"]["default.conf.template"]


def _hsts_map(conf: str) -> str:
    match = re.search(r"map [^{]*\$preloop_hsts \{(.*?)\n\}", conf, re.DOTALL)
    assert match, "no $preloop_hsts map in the rendered config"
    return match.group(1)


def test_every_rendered_block_with_add_header_has_the_security_set() -> None:
    """protects=criterion 3 of #2278 on the config Kubernetes installs run;
    fails_when=any rendered location or if block with its own add_header lacks
    one of the security headers, or values differ from the Docker snippet;
    no_claim=nginx runtime behaviour (smoke test covers that for Docker)."""
    conf = _rendered_nginx()
    expected = parse_header_lines(DOCKER_SNIPPET.read_text())
    blocks = [
        (header, body)
        for header, body in blocks_with_own_add_header(conf)
        if header.startswith(("location", "if", "server"))
    ]
    assert len(blocks) >= 7
    for header, body in blocks:
        headers: Dict[str, str] = parse_header_lines(body)
        for name, value in expected.items():
            assert headers.get(name) == value, f"{header}: {name}"
    assert "X-XSS-Protection" not in conf


def test_values_declare_hsts_defaults() -> None:
    """protects=the documented defaults (on, 1 year, no subdomains, no preload);
    fails_when=a default flips silently; no_claim=rendering."""
    values = load_values()
    assert resolve_values_path(values, "securityHeaders.hsts") == {
        "enabled": True,
        "maxAge": 31536000,
        "includeSubDomains": False,
        "preload": False,
    }


def test_rendered_map_sends_hsts_only_for_https_by_default() -> None:
    """protects=HSTS on HTTPS requests, none on plain HTTP;
    fails_when=the map lacks the forwarded-proto/scheme https branches or
    has a non-empty default; no_claim=nginx evaluation of the map."""
    conf = _rendered_nginx()
    body = _hsts_map(conf)
    assert '"max-age=31536000"' in body
    assert re.search(r'default\s+"";', body)
    assert "includeSubDomains" not in body
    assert "proxy_hide_header Strict-Transport-Security;" in conf


def test_rendered_map_is_empty_when_disabled() -> None:
    """protects=the opt-out; fails_when=disabling still yields a header value;
    no_claim=browsers that cached an earlier policy."""
    body = _hsts_map(_rendered_nginx(["securityHeaders.hsts.enabled=false"]))
    assert "max-age" not in body
    assert re.search(r'default\s+"";', body)


def test_rendered_map_honours_custom_values() -> None:
    """protects=criterion 4 of #2279 for Helm; fails_when=maxAge or
    includeSubDomains is ignored; no_claim=none beyond protects."""
    body = _hsts_map(
        _rendered_nginx(
            [
                "securityHeaders.hsts.maxAge=63072000",
                "securityHeaders.hsts.includeSubDomains=true",
            ]
        )
    )
    assert '"max-age=63072000; includeSubDomains"' in body


@pytest.mark.parametrize(
    "overrides",
    [
        ["securityHeaders.hsts.preload=true"],
        [
            "securityHeaders.hsts.preload=true",
            "securityHeaders.hsts.includeSubDomains=true",
            "securityHeaders.hsts.maxAge=600",
        ],
    ],
)
def test_helm_template_fails_on_invalid_preload(overrides: list[str]) -> None:
    """protects=criterion 5 of #2279; fails_when=helm renders a preload policy
    browsers would reject; no_claim=the API-side check (unit tests)."""
    helm = pytest.importorskip("shutil").which("helm")
    if helm is None:
        pytest.skip("helm binary not available")
    with offline_chart() as chart_dir:
        command = [
            helm,
            "template",
            "preloop",
            str(chart_dir),
            "--show-only",
            CONFIGMAP,
            "--set",
            f"environment.jwtSecret={TEST_JWT_SECRET}",
        ]
        for override in overrides:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "securityHeaders.hsts.preload" in result.stderr
    assert "includeSubDomains" in result.stderr
    assert "maxAge" in result.stderr


@pytest.mark.parametrize("max_age", ["abc", "-5", "31536000s", "1.5"])
def test_helm_template_fails_on_invalid_max_age(max_age: str) -> None:
    """protects=a typo in maxAge failing the render; fails_when=Sprig int64
    turns it into max-age=0 and silently disables HSTS; no_claim=none beyond
    protects."""
    helm = pytest.importorskip("shutil").which("helm")
    if helm is None:
        pytest.skip("helm binary not available")
    with offline_chart() as chart_dir:
        result = subprocess.run(
            [
                helm,
                "template",
                "preloop",
                str(chart_dir),
                "--show-only",
                CONFIGMAP,
                "--set",
                f"environment.jwtSecret={TEST_JWT_SECRET}",
                "--set",
                f"securityHeaders.hsts.maxAge={max_age}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode != 0
    assert "securityHeaders.hsts.maxAge must be a whole number" in result.stderr


def test_rendered_referrer_policy_defers_to_upstream() -> None:
    """protects=the API's no-referrer on token-bearing responses;
    fails_when=nginx adds a second, weaker Referrer-Policy when the upstream
    sent one; no_claim=runtime evaluation (smoke test)."""
    conf = _rendered_nginx()
    match = re.search(
        r"map \$upstream_http_referrer_policy \$preloop_referrer_policy \{(.*?)\n\}",
        conf,
        re.DOTALL,
    )
    assert match
    assert re.search(r'""\s+"strict-origin-when-cross-origin";', match.group(1))
    assert re.search(r'default\s+"";', match.group(1))


def test_preload_renders_when_valid() -> None:
    """protects=a valid preload configuration still renders;
    fails_when=the guard rejects valid input; no_claim=preload list submission."""
    body = _hsts_map(
        _rendered_nginx(
            [
                "securityHeaders.hsts.preload=true",
                "securityHeaders.hsts.includeSubDomains=true",
            ]
        )
    )
    assert '"max-age=31536000; includeSubDomains; preload"' in body


@pytest.mark.parametrize(
    "template", ["templates/api-deployment.yaml", "templates/gateway-deployment.yaml"]
)
def test_api_and_gateway_receive_hsts_env(template: str) -> None:
    """protects=the API middleware reading the same settings as nginx;
    fails_when=a values change does not reach the API or gateway pods;
    no_claim=the middleware's behaviour (unit tests)."""
    rendered = helm_template(
        template,
        ["securityHeaders.hsts.enabled=false", "securityHeaders.hsts.maxAge=600"],
    )
    env = {
        item["name"]: item.get("value")
        for item in yaml.safe_load(rendered)["spec"]["template"]["spec"]["containers"][
            0
        ]["env"]
    }
    assert env["HSTS_ENABLED"] == "false"
    assert env["HSTS_MAX_AGE"] == "600"
    assert env["HSTS_INCLUDE_SUBDOMAINS"] == "false"
    assert env["HSTS_PRELOAD"] == "false"
