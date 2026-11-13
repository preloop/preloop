"""Rendered-Helm guards for the console Content-Security-Policy (#2280)."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Dict, List

import pytest
import yaml

from tests.helm.chart_helpers import (
    TEST_JWT_SECRET,
    helm_template,
    load_values,
    offline_chart,
    resolve_values_path,
)
from tests.test_nginx_csp import (
    CONSOLE_FLAG_CASES,
    LEGACY_CSP,
    console_flag,
    directives,
    docker_effective_policy,
    needs_sh,
)

CONFIGMAP = "templates/configmap-nginx.yaml"
ANALYTICS = [
    "analytics.enabled=true",
    "analytics.domain=console.example.test",
    "analytics.scriptUrl=https://traffic.example.test/js/script.js",
]


def _conf(overrides: List[str] | None = None, strings: List[str] | None = None) -> str:
    rendered = helm_template(CONFIGMAP, overrides, string_overrides=strings)
    return yaml.safe_load(rendered)["data"]["default.conf.template"]


def _policy(conf: str) -> str:
    match = re.search(r'map "" \$preloop_csp_policy \{\s*default "([^"]+)";', conf)
    assert match, "no $preloop_csp_policy map in the rendered config"
    return match.group(1)


def _mode_map(conf: str, variable: str) -> Dict[str, str]:
    match = re.search(r'map "([^"]*)" \$' + variable + r" \{(.*?)\n\}", conf, re.DOTALL)
    assert match, f"no map for ${variable}"
    entries = {"_source": match.group(1)}
    for key, value in re.findall(
        r'^\s*("[^"]*"|default)\s+(.+?);\s*$', match.group(2), re.MULTILINE
    ):
        entries[key.strip('"')] = value
    return entries


def _helm_failure(overrides: List[str]) -> str:
    helm = shutil.which("helm")
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
            command += ["--set-string", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0, result.stdout
    return result.stderr


def test_values_declare_csp_defaults() -> None:
    """protects=the documented defaults (report-only, no extra sources);
    fails_when=the default mode flips to enforce in this release;
    no_claim=rendering."""
    assert resolve_values_path(load_values(), "securityHeaders.csp") == {
        "mode": "report-only",
        "extraScriptSrc": "",
        "extraConnectSrc": "",
    }


@pytest.mark.parametrize(
    ("mode", "enforcing", "report_only"),
    [
        ("report-only", f'"{LEGACY_CSP}"', "$preloop_csp_policy"),
        ("enforce", "$preloop_csp_policy", '""'),
        ("legacy", f'"{LEGACY_CSP}"', '""'),
    ],
)
def test_rendered_maps_pick_headers_per_mode(
    mode: str, enforcing: str, report_only: str
) -> None:
    """protects=acceptance criteria 1 and 2 of #2280 on Kubernetes installs;
    fails_when=a mode renders the wrong value for either header on console
    responses, or proxied responses or the /admin SPA lose the legacy
    header; no_claim=nginx map evaluation at runtime (the Docker smoke test
    runs real nginx)."""
    conf = _conf([f"securityHeaders.csp.mode={mode}"])
    csp = _mode_map(conf, "preloop_csp")
    ro = _mode_map(conf, "preloop_csp_report_only")
    assert csp["_source"] == ro["_source"] == f"{mode}:$preloop_csp_console"
    hit = f"{mode}:1"
    assert csp.get(hit, csp["default"]) == enforcing
    assert ro.get(hit, ro["default"]) == report_only
    # Proxied responses and /admin ($preloop_csp_console = 0) keep the legacy
    # header only.
    assert csp["default"] == f'"{LEGACY_CSP}"'
    assert ro["default"] == '""'
    for key, flag in CONSOLE_FLAG_CASES:
        assert console_flag(conf, key) == flag, key


def test_rendered_enforced_script_src_has_no_inline_or_scheme_sources() -> None:
    """protects=acceptance criterion 2 of #2280 for Helm, analytics on and off;
    fails_when=script-src gains 'unsafe-inline' or a scheme source;
    no_claim=browser behaviour (Playwright sweep)."""
    for overrides in ([], ANALYTICS):
        sources = dict(directives(_policy(_conf(overrides))))["script-src"].split()
        assert "'unsafe-inline'" not in sources
        assert not [s for s in sources if re.fullmatch(r"[a-z][a-z0-9+.-]*:", s)]


def test_extra_sources_land_in_their_directives() -> None:
    """protects=acceptance criterion 5 of #2280 for Helm;
    fails_when=extraScriptSrc or extraConnectSrc is dropped, lands in the
    wrong directive, or keeps stray whitespace; no_claim=none beyond
    protects."""
    found = dict(
        directives(
            _policy(
                _conf(
                    strings=[
                        "securityHeaders.csp.extraScriptSrc= https://example.test  *.cdn.example.test",
                        "securityHeaders.csp.extraConnectSrc=wss://events.example.test",
                    ]
                )
            )
        )
    )
    assert found["script-src"] == "'self' https://example.test *.cdn.example.test"
    assert found["connect-src"] == "'self' ws: wss: data: wss://events.example.test"


def test_analytics_origin_and_bootstrap_file() -> None:
    """protects=acceptance criterion 4 of #2280 on the Helm side: the tracker
    origin is allowed for script and events, and the queue stub is a file;
    fails_when=the origin is missing from script-src or connect-src, or the
    sub_filter still injects inline script; no_claim=that the tracker runs
    (the Playwright analytics test does that with a local stub)."""
    conf = _conf(ANALYTICS)
    found = dict(directives(_policy(conf)))
    assert found["script-src"] == "'self' https://traffic.example.test"
    assert found["connect-src"] == "'self' ws: wss: data: https://traffic.example.test"
    sub_filter = re.search(r"sub_filter '</head>' '(.*)</head>';", conf)
    assert sub_filter
    injected = sub_filter.group(1)
    assert injected.startswith('<script src="/analytics-queue.js"></script>')
    assert "window.plausible" not in injected
    # Case-insensitive: an upper-case <SCRIPT> tag would run inline too.
    assert re.findall(r"<script\b[^>]*>", injected, re.IGNORECASE) == [
        '<script src="/analytics-queue.js">',
        '<script defer data-domain="console.example.test" '
        'src="https://traffic.example.test/js/script.js">',
    ]


def test_custom_snippet_adds_no_origin_automatically() -> None:
    """protects=the documented custom-snippet contract (operator lists its
    origins in extraScriptSrc); fails_when=the chart guesses an origin from
    scriptUrl for a snippet it cannot parse; no_claim=that the snippet itself
    works under enforce (inline code in it is blocked, by design)."""
    conf = _conf(
        ANALYTICS + ["securityHeaders.csp.extraScriptSrc=https://matomo.example.test"],
        strings=[
            'analytics.customSnippet=<script src="https://matomo.example.test/m.js"></script>'
        ],
    )
    found = dict(directives(_policy(conf)))
    assert found["script-src"] == "'self' https://matomo.example.test"
    assert found["connect-src"] == "'self' ws: wss: data:"


@pytest.mark.parametrize("mode", ["strict", "Enforce", "off", ""])
def test_helm_template_fails_on_unknown_mode(mode: str) -> None:
    """protects=acceptance criterion 6 of #2280 for Helm;
    fails_when=an unknown mode renders with some default;
    no_claim=none beyond protects."""
    if mode == "":
        # An empty string means "use the default", like an unset env var.
        assert "report-only:$preloop_csp_console" in _conf(
            strings=["securityHeaders.csp.mode="]
        )
        return
    stderr = _helm_failure([f"securityHeaders.csp.mode={mode}"])
    assert (
        "securityHeaders.csp.mode must be one of report-only, enforce, legacy" in stderr
    )


@pytest.mark.parametrize(
    "value", ["'unsafe-inline'", "https:", "*", "https://a.test;script-src", "$host"]
)
def test_helm_template_fails_on_non_host_sources(value: str) -> None:
    """protects=the policy against an extra source that reopens script-src or
    breaks out of the header; fails_when=Helm renders a keyword, bare scheme,
    wildcard, semicolon or nginx variable; no_claim=that a listed host is
    trustworthy."""
    for key in ("extraScriptSrc", "extraConnectSrc"):
        stderr = _helm_failure([f"securityHeaders.csp.{key}={value}"])
        assert f"securityHeaders.csp.{key} must be space-separated host sources" in (
            stderr
        )


@needs_sh
@pytest.mark.parametrize(
    ("helm", "env"),
    [
        ([], {}),
        (
            [
                "securityHeaders.csp.extraScriptSrc=https://example.test",
                "securityHeaders.csp.extraConnectSrc=wss://events.example.test",
            ],
            {
                "CSP_EXTRA_SCRIPT_SRC": "https://example.test",
                "CSP_EXTRA_CONNECT_SRC": "wss://events.example.test",
            },
        ),
    ],
)
def test_helm_and_docker_send_the_same_policy(
    helm: List[str], env: Dict[str, str]
) -> None:
    """protects=one policy across Docker and Helm installs;
    fails_when=either template is edited and the other is not, or the two
    build the extra sources differently; no_claim=analytics (Docker has no
    analytics injection)."""
    assert _policy(_conf(strings=helm)) == docker_effective_policy(env)


@pytest.mark.parametrize(
    ("script_url", "origin"),
    [
        ("/js/script.js", None),
        ("//cdn.example.test/js/script.js", "cdn.example.test"),
        ("http://127.0.0.1:8000/js/script.js", "http://127.0.0.1:8000"),
    ],
)
def test_analytics_script_url_forms(script_url: str, origin: str | None) -> None:
    """protects=analytics configurations that rendered before this change: a
    tracker proxied under the console's domain (relative URL) and a
    protocol-relative URL; fails_when=either fails the render, a relative URL
    adds a source, or a protocol-relative one loses its host;
    no_claim=that the tracker is reachable at that URL."""
    conf = _conf(
        [
            "analytics.enabled=true",
            "analytics.domain=console.example.test",
            f"analytics.scriptUrl={script_url}",
        ]
    )
    found = dict(directives(_policy(conf)))
    extra = f" {origin}" if origin else ""
    assert found["script-src"] == f"'self'{extra}"
    assert found["connect-src"] == f"'self' ws: wss: data:{extra}"
    assert f'src="{script_url}"' in conf
