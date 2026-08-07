"""Route parity between the two nginx configs.

This repo serves the marketing site from two independently maintained nginx
configs:

* ``frontend/docker/nginx.conf.template`` - docker-compose and the standalone
  image.
* ``helm/preloop/templates/configmap-nginx.yaml`` - **production preloop.ai**,
  mounted into ``preloop-console`` at ``/etc/nginx/templates``.

They have drifted twice, both times the same way: a prerendered route was added
to the docker template only, so production quietly fell through to the SPA
catch-all and returned homepage HTML with a 200 (not a 404, which is why it
went unnoticed).

1. ``/about|/pricing|/privacy|...`` - documented in a comment in the Helm file.
2. ``/blog``, ``/blog/<slug>``, ``/blog/feed.xml`` - added in 4c17027e, missing
   from Helm; preloop.ai served the homepage for every blog URL.

A prose comment did not prevent the second recurrence, so this asserts it.

The comparison is on **behaviour, not text**: the two configs legitimately
spell the same routing differently (Helm folds the marketing pages into one
alternation regex, docker lists them individually). So this implements nginx's
location-matching precedence and compares which config block each URL lands in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_TEMPLATE = REPO_ROOT / "frontend" / "docker" / "nginx.conf.template"
HELM_CONFIGMAP = REPO_ROOT / "helm" / "preloop" / "templates" / "configmap-nginx.yaml"

# Marketing URLs that must serve prerendered HTML rather than the SPA shell.
# Extensionless on purpose: these are the ones the catch-all silently swallows.
# Files with extensions (/robots.txt, /sitemap.xml, /llms.txt) are served by
# `try_files $uri` in both configs and are not a drift risk.
PRERENDERED_URLS = (
    "/about",
    "/pricing",
    "/privacy",
    "/terms",
    "/whatis-mcp",
    "/blog",
    "/blog/govern-your-qm-fleet",
    "/vs/litellm",
    "/resources/ai-agent-control-plane-2026",
)

# URLs that must keep falling through to the SPA shell.
SPA_URLS = ("/", "/console", "/blog/no-such-post-exists")

_LOCATION = re.compile(r"^[ \t]*location\s+(?P<matcher>[^{]+?)\s*\{", re.MULTILINE)


def _read_docker_template() -> str:
    return DOCKER_TEMPLATE.read_text(encoding="utf-8")


def _read_helm_server_block() -> str:
    """Extract the nginx template string out of the Helm ConfigMap.

    The file is a Go template and therefore not valid YAML as-is. Blanking the
    ``{{ ... }}`` actions is enough to recover ``default.conf.template``, which
    is all this test inspects.
    """
    raw = HELM_CONFIGMAP.read_text(encoding="utf-8")
    without_actions = re.sub(r"\{\{-?.*?-?\}\}", "", raw, flags=re.DOTALL)
    doc = yaml.safe_load(without_actions)
    return doc["data"]["default.conf.template"]


def _location_body(config: str, start: int) -> str:
    """Return the text of one balanced ``{ ... }`` block, given the index after `{`."""
    depth = 1
    index = start
    while index < len(config) and depth:
        char = config[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return config[start : index - 1]


def _parse_locations(config: str) -> list[tuple[str, str, str]]:
    """Return ``(kind, pattern, body)`` for each location block, in file order.

    ``kind`` is one of ``exact``, ``prefix_priority`` (``^~``), ``regex``
    (``~`` / ``~*``) or ``prefix``.
    """
    parsed: list[tuple[str, str, str]] = []
    for match in _LOCATION.finditer(config):
        matcher = " ".join(match.group("matcher").split())
        body = _location_body(config, match.end())
        if matcher.startswith("= "):
            parsed.append(("exact", matcher[2:].strip(), body))
        elif matcher.startswith("^~ "):
            parsed.append(("prefix_priority", matcher[3:].strip(), body))
        elif matcher.startswith("~* "):
            parsed.append(("regex", matcher[3:].strip(), body))
        elif matcher.startswith("~ "):
            parsed.append(("regex", matcher[2:].strip(), body))
        else:
            parsed.append(("prefix", matcher.strip(), body))
    return parsed


def _resolve(config: str, url: str) -> str | None:
    """Return the body of the location block nginx would use for ``url``.

    Implements nginx precedence: exact ``=`` wins; then the longest matching
    prefix, and if that prefix is ``^~`` it wins outright; otherwise regexes are
    tried in file order; otherwise the longest prefix match applies.
    """
    locations = _parse_locations(config)

    for kind, pattern, body in locations:
        if kind == "exact" and pattern == url:
            return body

    best_prefix: tuple[int, str, str] | None = None
    for kind, pattern, body in locations:
        if kind in ("prefix", "prefix_priority") and url.startswith(pattern):
            if best_prefix is None or len(pattern) > best_prefix[0]:
                best_prefix = (len(pattern), kind, body)

    if best_prefix and best_prefix[1] == "prefix_priority":
        return best_prefix[2]

    for kind, pattern, body in locations:
        if kind == "regex" and re.search(pattern, url):
            return body

    return best_prefix[2] if best_prefix else None


def _serves_prerendered(body: str, url: str) -> bool:
    """True when the block serves a static file for ``url`` before any SPA fallback.

    Both configs express this as ``try_files /<something> ... /index.html``: the
    distinguishing feature is a first argument that is a rooted static path
    rather than ``$uri``.
    """
    match = re.search(r"try_files\s+(?P<args>[^;]+);", body)
    if not match:
        return False
    first = match.group("args").split()[0]
    return first.startswith("/") and first != "/index.html"


@pytest.fixture(scope="module")
def configs() -> dict[str, str]:
    return {"docker": _read_docker_template(), "helm": _read_helm_server_block()}


def test_both_nginx_configs_exist() -> None:
    assert DOCKER_TEMPLATE.is_file(), f"missing {DOCKER_TEMPLATE}"
    assert HELM_CONFIGMAP.is_file(), f"missing {HELM_CONFIGMAP}"


def test_helm_configmap_parses(configs: dict[str, str]) -> None:
    assert "server {" in configs["helm"]
    assert _parse_locations(configs["helm"]), "no location blocks parsed from Helm"


@pytest.mark.parametrize("url", PRERENDERED_URLS)
def test_marketing_urls_serve_prerendered_html_in_both_configs(
    configs: dict[str, str], url: str
) -> None:
    """A route prerendered in one config must be prerendered in the other.

    Production mounts the Helm ConfigMap. A route wired up only in the docker
    template works locally and silently serves the SPA homepage on preloop.ai
    with a 200, which is exactly how the /blog outage stayed invisible.
    """
    for name, config in configs.items():
        body = _resolve(config, url)
        assert body is not None, f"{name}: no location block matches {url}"
        assert _serves_prerendered(body, url), (
            f"{name} nginx config does not serve prerendered HTML for {url}; "
            "it falls through to the SPA catch-all, which returns the homepage "
            "with a 200. Add the route to "
            f"{'helm/preloop/templates/configmap-nginx.yaml' if name == 'helm' else 'frontend/docker/nginx.conf.template'}."
        )


@pytest.mark.parametrize("url", SPA_URLS)
def test_spa_fallback_preserved_in_both_configs(
    configs: dict[str, str], url: str
) -> None:
    """Unknown and app routes must still reach the SPA shell."""
    for name, config in configs.items():
        body = _resolve(config, url)
        assert body is not None, f"{name}: no location block matches {url}"
        assert "/index.html" in body, (
            f"{name} nginx config no longer falls back to the SPA for {url}"
        )


def test_blog_index_uses_exact_match(configs: dict[str, str]) -> None:
    """`/blog` needs `location = /blog`, not a prefix match.

    With only a prefix match the catch-all's ``try_files $uri $uri/`` finds the
    ``blog`` *directory* and issues ``301 -> /blog/``. The canonical URL, the
    sitemap entry and every ``rel=alternate`` use the slash-less ``/blog``, so
    the published URL would never serve the index.
    """
    for name, config in configs.items():
        kinds = {(kind, pattern) for kind, pattern, _ in _parse_locations(config)}
        assert ("exact", "/blog") in kinds, (
            f"{name} nginx config lost `location = /blog` (exact match); "
            "a prefix match 301s to /blog/ and breaks the canonical URL."
        )


def test_blog_feed_is_served_as_rss(configs: dict[str, str]) -> None:
    """`default_type` alone is not enough for .xml; an empty `types` block is required.

    nginx's mime.types already maps ``.xml`` -> ``text/xml``. ``default_type``
    only applies when *nothing* matched, so without ``types { }`` the feed is
    served as text/xml. Verified against nginx:alpine.
    """
    for name, config in configs.items():
        body = _resolve(config, "/blog/feed.xml")
        assert body is not None, f"{name}: no location block matches /blog/feed.xml"
        assert "application/rss+xml" in body, (
            f"{name} feed location does not set application/rss+xml"
        )
        assert re.search(r"types\s*\{\s*\}", body), (
            f"{name} feed location sets default_type without an empty `types {{ }}` "
            "block, so nginx's mime.types wins and the feed is served as text/xml."
        )
