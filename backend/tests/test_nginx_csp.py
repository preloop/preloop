"""Console Content-Security-Policy in the Docker image (#2280).

The Docker image's nginx picks its CSP headers from ``CSP_MODE`` through maps
in ``frontend/docker/nginx.conf.template``; ``frontend/docker/
15-preloop-csp.envsh`` (sourced by the nginx entrypoint before envsubst)
validates the settings and builds the script-src and connect-src values.
These tests read the template as text and run the envsh with ``sh``; the
real-nginx check is ``tests/test_nginx_headers_smoke.py`` and the Helm side
is ``tests/helm/test_nginx_csp_render.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Tuple

import pytest

from tests.test_nginx_security_headers import (
    DOCKER_SNIPPET,
    DOCKER_TEMPLATE,
    DOCKERFILE,
    REPO_ROOT,
    parse_header_lines,
)

ENVSH = REPO_ROOT / "frontend" / "docker" / "15-preloop-csp.envsh"
LEGACY_CSP = "default-src 'self' http: https: data: blob: 'unsafe-inline'"
MODES = ("report-only", "enforce", "legacy")

# The policy from #2280 plus what the violation sweep added: data: in
# connect-src (Shoelace fetches its built-in system icons as data: URLs) and
# the privacy-enhanced video embed origin in frame-src.
EXPECTED_DIRECTIVES: List[Tuple[str, str]] = [
    ("default-src", "'self'"),
    ("script-src", "${PRELOOP_CSP_SCRIPT_SRC}"),
    ("style-src", "'self' 'unsafe-inline'"),
    ("img-src", "'self' data: blob: https:"),
    ("font-src", "'self' data:"),
    ("connect-src", "${PRELOOP_CSP_CONNECT_SRC}"),
    ("media-src", "'self' blob:"),
    ("worker-src", "'self' blob:"),
    ("frame-src", "https://www.youtube-nocookie.com"),
    ("object-src", "'none'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("frame-ancestors", "'self'"),
]


def directives(policy: str) -> List[Tuple[str, str]]:
    """Split a policy into ``(name, sources)`` pairs, in order."""
    pairs = []
    for part in policy.split(";"):
        name, _, sources = part.strip().partition(" ")
        pairs.append((name, " ".join(sources.split())))
    return pairs


def docker_policy_template() -> str:
    """The new policy as written in nginx.conf.template, placeholders intact."""
    match = re.search(
        r'map "" \$preloop_csp_policy \{\s*default "([^"]+)";',
        DOCKER_TEMPLATE.read_text(),
    )
    assert match, "no $preloop_csp_policy map in nginx.conf.template"
    return match.group(1)


def mode_map(variable: str, text: str) -> Dict[str, str]:
    """Return ``{key: value}`` of the CSP_MODE map that sets ``variable``."""
    match = re.search(r'map "([^"]*)" \$' + variable + r" \{(.*?)\n\}", text, re.DOTALL)
    assert match, f"no map for ${variable}"
    assert match.group(1).endswith(":$preloop_csp_console")
    entries = {}
    for key, value in re.findall(
        r'^\s*("[^"]*"|default)\s+(.+?);\s*$', match.group(2), re.MULTILINE
    ):
        entries[key.strip('"')] = value
    return entries


def console_flag(text: str, key: str) -> str:
    """Evaluate the ``$preloop_csp_console`` map for ``$upstream_addr:$uri``.

    nginx checks the map's regex keys in order and falls back to ``default``;
    the keys here are plain PCRE that Python's ``re`` reads the same way.
    """
    match = re.search(
        r'map "\$upstream_addr:\$uri" \$preloop_csp_console \{(.*?)\n\}',
        text,
        re.DOTALL,
    )
    assert match, "no $preloop_csp_console map keyed on $upstream_addr:$uri"
    default = None
    for raw_key, value in re.findall(
        r'^\s*("[^"]*"|default)\s+(\S+);\s*$', match.group(1), re.MULTILINE
    ):
        if raw_key == "default":
            default = value
            continue
        pattern = raw_key.strip('"')
        assert pattern.startswith("~"), f"non-regex key {pattern}"
        if re.search(pattern[1:], key):
            return value
    assert default is not None
    return default


# (``$upstream_addr:$uri``, expected flag): 1 gets the new policy.
CONSOLE_FLAG_CASES = [
    (":/index.html", "1"),
    (":/vs/aws-agentcore.html", "1"),
    (":/assets/main.js", "1"),
    (":/administrator.html", "1"),
    (":/admin", "0"),
    (":/admin/index.html", "0"),
    (":/admin/assets/app.js", "0"),
    ("10.0.0.5:80:/api/v1/health", "0"),
    ("10.0.0.5:80:/approval/abc", "0"),
]


def run_envsh(env: Dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Source the envsh the way the nginx entrypoint does and print its output."""
    script = (
        f'. "{ENVSH}"\n'
        'printf "%s\\n%s\\n%s\\n" "$CSP_MODE" "$PRELOOP_CSP_SCRIPT_SRC" '
        '"$PRELOOP_CSP_CONNECT_SRC"\n'
    )
    clean = {
        key: value for key, value in os.environ.items() if not key.startswith("CSP_")
    }
    return subprocess.run(
        ["sh", "-c", script],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def docker_effective_policy(env: Dict[str, str]) -> str:
    """The policy nginx would send for ``env``, built as the image builds it."""
    result = run_envsh(env)
    assert result.returncode == 0, result.stderr
    _, script_src, connect_src = result.stdout.splitlines()
    return (
        docker_policy_template()
        .replace("${PRELOOP_CSP_SCRIPT_SRC}", script_src)
        .replace("${PRELOOP_CSP_CONNECT_SRC}", connect_src)
    )


def test_docker_policy_has_the_specified_directives() -> None:
    """protects=the directive list of #2280 in the Docker image;
    fails_when=a directive is dropped, loosened or reordered;
    no_claim=what browsers do with it (the Playwright sweep)."""
    assert directives(docker_policy_template()) == EXPECTED_DIRECTIVES


def test_docker_snippet_sends_both_csp_headers_from_the_maps() -> None:
    """protects=the two CSP header names on every response the snippet covers;
    fails_when=the snippet hard-codes a policy or drops the report-only header;
    no_claim=nginx evaluation (smoke test)."""
    headers = parse_header_lines(DOCKER_SNIPPET.read_text())
    assert headers["Content-Security-Policy"] == "$preloop_csp"
    assert headers["Content-Security-Policy-Report-Only"] == "$preloop_csp_report_only"


def test_docker_maps_pick_headers_per_mode() -> None:
    """protects=acceptance criteria 1 and 2 of #2280 (report-only keeps the
    legacy header and adds the new one; enforce sends only the new one;
    legacy only the old one); fails_when=a mode sends the wrong header set,
    or a proxied API response or the /admin SPA gets the new policy;
    no_claim=nginx map evaluation at runtime (smoke test)."""
    text = DOCKER_TEMPLATE.read_text()
    for key, flag in CONSOLE_FLAG_CASES:
        assert console_flag(text, key) == flag, key
    enforcing = mode_map("preloop_csp", text)
    report_only = mode_map("preloop_csp_report_only", text)
    assert enforcing == {
        "enforce:1": "$preloop_csp_policy",
        "default": f'"{LEGACY_CSP}"',
    }
    assert report_only == {"report-only:1": "$preloop_csp_policy", "default": '""'}
    assert 'map "${CSP_MODE}:$preloop_csp_console" $preloop_csp {' in text


def test_dockerfile_defaults_to_report_only_and_installs_the_envsh() -> None:
    """protects=the default (nothing blocked on upgrade) and the validation
    step being in the image; fails_when=the default flips or the envsh is not
    copied into /docker-entrypoint.d as an executable; no_claim=none beyond
    protects."""
    dockerfile = DOCKERFILE.read_text()
    assert "ENV CSP_MODE=report-only" in dockerfile
    assert 'ENV CSP_EXTRA_SCRIPT_SRC=""' in dockerfile
    assert 'ENV CSP_EXTRA_CONNECT_SRC=""' in dockerfile
    assert (
        "COPY docker/15-preloop-csp.envsh /docker-entrypoint.d/15-preloop-csp.envsh"
        in dockerfile
    )
    assert "chmod 755 /docker-entrypoint.d/15-preloop-csp.envsh" in dockerfile
    assert os.access(ENVSH, os.X_OK)


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


@needs_sh
def test_envsh_defaults() -> None:
    """protects=the default policy sources; fails_when=an unset CSP_MODE is
    not report-only or the default sources change; no_claim=busybox sh in
    the image (smoke test runs the real entrypoint)."""
    result = run_envsh({})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "report-only",
        "'self'",
        "'self' ws: wss: data:",
    ]


@needs_sh
def test_envsh_appends_normalised_extra_sources() -> None:
    """protects=acceptance criterion 5 of #2280 for Docker (extra origins land
    in script-src / connect-src); fails_when=an origin is dropped, the list
    keeps stray whitespace, or the wrong directive gets it; no_claim=none
    beyond protects."""
    policy = docker_effective_policy(
        {
            "CSP_MODE": "enforce",
            "CSP_EXTRA_SCRIPT_SRC": "  https://example.test   *.cdn.example.test ",
            "CSP_EXTRA_CONNECT_SRC": "wss://events.example.test 127.0.0.1:4000",
        }
    )
    found = dict(directives(policy))
    assert found["script-src"] == "'self' https://example.test *.cdn.example.test"
    assert found["connect-src"] == (
        "'self' ws: wss: data: wss://events.example.test 127.0.0.1:4000"
    )


@needs_sh
def test_envsh_enforced_script_src_has_no_inline_or_scheme_sources() -> None:
    """protects=acceptance criterion 2 of #2280 (enforced script-src has no
    'unsafe-inline' and no http:/https: scheme source); fails_when=the
    template or the envsh adds one; no_claim=sources a future directive
    might inherit through default-src."""
    sources = dict(directives(docker_effective_policy({"CSP_MODE": "enforce"})))[
        "script-src"
    ].split()
    assert "'unsafe-inline'" not in sources
    assert not [s for s in sources if re.fullmatch(r"[a-z][a-z0-9+.-]*:", s)]


@needs_sh
@pytest.mark.parametrize("mode", ["strict", "Enforce", "report_only", "off"])
def test_envsh_rejects_unknown_mode(mode: str) -> None:
    """protects=acceptance criterion 6 of #2280 for Docker (bad CSP_MODE stops
    the container with the three valid values); fails_when=an unknown value
    starts with some default; no_claim=the entrypoint's handling of a failing
    envsh (smoke test)."""
    result = run_envsh({"CSP_MODE": mode})
    assert result.returncode != 0
    assert "CSP_MODE must be one of report-only, enforce, legacy" in result.stderr
    assert mode in result.stderr


@needs_sh
@pytest.mark.parametrize(
    "value",
    [
        "'unsafe-inline'",
        "https:",
        "*",
        "https://a.test; script-src *",
        'https://a.test"',
        "https://$host",
        "data:",
    ],
)
def test_envsh_rejects_non_host_sources(value: str) -> None:
    """protects=the policy against an extra source that reopens script-src or
    breaks out of the header; fails_when=a keyword, a bare scheme, a
    wildcard, a quote, a semicolon or an nginx variable is accepted;
    no_claim=that a listed host is trustworthy."""
    for key in ("CSP_EXTRA_SCRIPT_SRC", "CSP_EXTRA_CONNECT_SRC"):
        result = run_envsh({key: value})
        assert result.returncode != 0, (key, value)
        assert f"{key} must be space-separated host sources" in result.stderr


def test_compose_console_services_forward_csp_settings() -> None:
    """protects=CSP_* in .env reaching the console container on Compose
    installs; fails_when=a console service does not forward a key or uses
    another default; no_claim=docker compose interpolation itself."""
    import yaml

    expected = {
        "CSP_MODE": "${CSP_MODE:-report-only}",
        "CSP_EXTRA_SCRIPT_SRC": "${CSP_EXTRA_SCRIPT_SRC:-}",
        "CSP_EXTRA_CONNECT_SRC": "${CSP_EXTRA_CONNECT_SRC:-}",
    }
    for compose, service in (
        ("docker-compose.release.yaml", "console"),
        ("frontend/docker-compose.yml", "frontend"),
    ):
        env = yaml.safe_load((REPO_ROOT / compose).read_text())["services"][service][
            "environment"
        ]
        if isinstance(env, list):
            env = dict(item.split("=", 1) for item in env)
        assert {key: env.get(key) for key in expected} == expected, compose
    text = (REPO_ROOT / ".env.example").read_text()
    for key in expected:
        assert f"# {key}=" in text
