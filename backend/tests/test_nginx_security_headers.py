"""Security headers in the two console nginx configs (#2278, #2279).

nginx does not inherit ``add_header`` from the enclosing level once a block
declares any ``add_header`` of its own. Both configs used to declare the
security headers at server level only, so the SPA shell (``= /index.html``),
static assets, ``/admin`` and the ``/api/`` CORS block shipped without them.

The fix defines the header set once per config (a snippet file for the Docker
image, a named template for Helm) and pulls it into every block that sets its
own headers. These tests parse the configs and check that structurally; the
rendered-Helm and real-nginx checks live in ``tests/helm`` and
``tests/test_nginx_headers_smoke.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_TEMPLATE = REPO_ROOT / "frontend" / "docker" / "nginx.conf.template"
DOCKER_SNIPPET = REPO_ROOT / "frontend" / "docker" / "security-headers.conf"
DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
HELM_CONFIGMAP = REPO_ROOT / "helm" / "preloop" / "templates" / "configmap-nginx.yaml"
HELM_HELPERS = REPO_ROOT / "helm" / "preloop" / "templates" / "_helpers.tpl"

SNIPPET_INCLUDE = "include /etc/nginx/snippets/security-headers.conf;"
HELM_INCLUDE = re.compile(
    r'(\{\{|\u27e6)-?\s*include\s+"preloop\.nginxSecurityHeaders"'
)

# The header set from #2278 plus HSTS from #2279. The CSP value is unchanged on
# purpose: tightening it is #2280.
EXPECTED_HEADERS: Dict[str, str] = {
    "X-Frame-Options": '"SAMEORIGIN"',
    "X-Content-Type-Options": '"nosniff"',
    # Default strict-origin-when-cross-origin, empty when a proxied upstream
    # set its own policy (see the map in nginx.conf.template).
    "Referrer-Policy": "$preloop_referrer_policy",
    "Permissions-Policy": (
        '"camera=(), geolocation=(), payment=(), usb=(), microphone=(self)"'
    ),
    "Content-Security-Policy": (
        "\"default-src 'self' http: https: data: blob: 'unsafe-inline'\""
    ),
    "Strict-Transport-Security": "$preloop_hsts",
}

_ADD_HEADER = re.compile(r"^\s*add_header\s+(?P<name>\S+)\s+(?P<value>.+?)\s+always;")


def parse_header_lines(text: str) -> Dict[str, str]:
    """Return ``{name: value}`` for every ``add_header ... always;`` line."""
    headers: Dict[str, str] = {}
    for line in text.splitlines():
        match = _ADD_HEADER.match(line)
        if match:
            headers[match.group("name").strip("'\"")] = match.group("value")
    return headers


def blocks_with_own_add_header(text: str) -> List[Tuple[str, str]]:
    """Return ``(header, direct_body)`` for blocks that declare ``add_header``.

    ``direct_body`` is the block's text with nested blocks removed, because
    nginx inheritance is decided per block: a nested ``if`` with its own
    ``add_header`` loses the enclosing ``location``'s headers too.
    """
    # Go-template actions use doubled braces; neutralise them so only nginx
    # braces count.
    text = text.replace("{{", "\u27e6").replace("}}", "\u27e7")
    found: List[Tuple[str, str]] = []
    stack: List[Tuple[str, List[str]]] = []
    line_start = 0
    for index, char in enumerate(text):
        if char == "\n":
            line_start = index + 1
        if char == "{":
            stack.append((text[line_start:index].strip(), []))
        elif char == "}" and stack:
            header, parts = stack.pop()
            body = "".join(parts)
            if re.search(r"^\s*add_header\s", body, re.MULTILINE) or (
                HELM_INCLUDE.search(body)
            ):
                found.append((header, body))
        elif stack:
            stack[-1][1].append(char)
    return found


def _location_blocks(text: str) -> List[Tuple[str, str]]:
    return [
        (header, body)
        for header, body in blocks_with_own_add_header(text)
        if header.startswith(("location", "if", "server"))
    ]


def test_docker_snippet_defines_the_exact_header_set() -> None:
    """protects=the header names and values the Docker image sends;
    fails_when=a header is missing, has another value, or X-XSS-Protection
    returns; no_claim=that nginx applies the snippet (see the smoke test)."""
    assert parse_header_lines(DOCKER_SNIPPET.read_text()) == EXPECTED_HEADERS


def test_every_docker_block_with_add_header_includes_the_snippet() -> None:
    """protects=security headers on the SPA shell, assets, /admin and /api/;
    fails_when=a block declares add_header without the snippet include, which
    silently drops the server-level headers; no_claim=runtime nginx behaviour,
    this is a text check of frontend/docker/nginx.conf.template."""
    text = DOCKER_TEMPLATE.read_text()
    blocks = _location_blocks(text)
    headers = [header for header, _ in blocks]
    for expected in (
        "location /api/",
        "if ($request_method = 'OPTIONS')",
        "location ^~ /admin/assets/",
        "location = /admin/index.html",
        "location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg)$",
        "location = /index.html",
    ):
        assert expected in headers, f"{expected} lost its add_header block"
    for header, body in blocks:
        assert SNIPPET_INCLUDE in body, f"{header} drops the security headers"
    # Server level carries the set too, for blocks without their own headers.
    server_body = text.split("\n    location", 1)[0]
    assert SNIPPET_INCLUDE in server_body
    assert parse_header_lines(server_body) == {}
    assert "COPY docker/security-headers.conf /etc/nginx/snippets/" in (
        DOCKERFILE.read_text()
    )


def test_every_helm_block_with_add_header_includes_the_named_template() -> None:
    """protects=the same coverage in the Helm console config;
    fails_when=a Helm location sets add_header without the named template;
    no_claim=what the template renders (tests/helm checks the rendered
    output)."""
    text = HELM_CONFIGMAP.read_text()
    blocks = _location_blocks(text)
    assert len([h for h, _ in blocks if h.startswith(("location", "if"))]) >= 6
    for header, body in blocks:
        assert HELM_INCLUDE.search(body), f"{header} drops the security headers"


def test_helm_named_template_matches_docker_snippet() -> None:
    """protects=identical header names and values across Docker and Helm;
    fails_when=one template is edited and the other is not;
    no_claim=Go-template evaluation, the helper is read as text."""
    helpers = HELM_HELPERS.read_text()
    match = re.search(
        r'\{\{-?\s*define\s+"preloop\.nginxSecurityHeaders"\s*-?\}\}(.*?)\{\{-?\s*end\s*-?\}\}',
        helpers,
        re.DOTALL,
    )
    assert match, "preloop.nginxSecurityHeaders is not defined"
    assert parse_header_lines(match.group(1)) == parse_header_lines(
        DOCKER_SNIPPET.read_text()
    )


def test_no_config_sends_x_xss_protection() -> None:
    """protects=removal of the deprecated X-XSS-Protection header;
    fails_when=any of the four files still mentions it;
    no_claim=headers added by an ingress in front of nginx."""
    for path in (DOCKER_TEMPLATE, DOCKER_SNIPPET, HELM_CONFIGMAP, HELM_HELPERS):
        assert "X-XSS-Protection" not in path.read_text(), path


def test_both_configs_hide_upstream_hsts_and_map_it_from_https() -> None:
    """protects=exactly one HSTS header on proxied responses and HSTS only for
    HTTPS requests; fails_when=proxy_hide_header or the X-Forwarded-Proto/scheme
    map is missing; no_claim=map semantics at runtime (smoke test)."""
    docker = DOCKER_TEMPLATE.read_text()
    server_level = docker.split("\n    location", 1)[0]
    assert "proxy_hide_header Strict-Transport-Security;" in server_level
    assert "$http_x_forwarded_proto" in docker.split("server {", 1)[0]
    assert "map " in docker.split("server {", 1)[0]
    assert re.search(
        r"map \$upstream_http_referrer_policy \$preloop_referrer_policy \{\s*"
        r'""\s+"strict-origin-when-cross-origin";\s*default\s+"";',
        docker,
    )
    for var in (
        "HSTS_ENABLED",
        "HSTS_MAX_AGE",
        "HSTS_INCLUDE_SUBDOMAINS",
        "HSTS_PRELOAD",
    ):
        assert "${" + var + "}" in docker
        assert f"ENV {var}=" in DOCKERFILE.read_text()
    helm = HELM_CONFIGMAP.read_text()
    assert "proxy_hide_header Strict-Transport-Security;" in helm
    assert 'include "preloop.nginxHstsMap"' in helm.split("server {", 1)[0]
    # No location may declare its own proxy_hide_header: that would reset the
    # inherited server-level hide and let a duplicate HSTS through.
    for text in (docker, helm):
        directives = re.findall(r"^\s*proxy_hide_header\s", text, re.MULTILINE)
        assert len(directives) == 1
